#!/usr/bin/env python3
"""Replay and verify the modern semantic patch against every audited tag."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from patches.legacy.apply_legacy_patch import (  # noqa: E402
    UPSTREAM_PROTECTION_TOKENS,
    upstream_protections,
)
from patches.modern.apply_modern_patch import (  # noqa: E402
    BASE_MEMORY_H,
    DESERIALIZER_CC,
    D8_CC,
    D8_H,
    OBJECT_DESERIALIZER_CC,
    PATCH_MARKER,
    PRINTER_CC,
    SERIALIZER_CC,
    SERIALIZER_H,
    SNAPSHOT_DATA_H,
    SOURCE_PATHS,
    STRING_CC,
    transform_sources,
)
from tools.audit_legacy_v8 import RawSourceCache, version_key  # noqa: E402


def protected_token_counts_unchanged(before: str, after: str) -> bool:
    return all(
        tuple(before.count(token) for token in tokens)
        == tuple(after.count(token) for token in tokens)
        for name, tokens in UPSTREAM_PROTECTION_TOKENS.items()
        if name != "read_only_snapshot_checksum"
    )


def validate_version(cache: RawSourceCache, version: str) -> dict:
    sources: dict[str, str] = {}
    try:
        for path in SOURCE_PATHS:
            content = cache.get(version, path)
            if content is not None:
                sources[path] = content
        transformed, features, changed = transform_sources(sources)
        d8 = transformed[D8_CC]
        serializer = transformed[SERIALIZER_CC]
        expected_constant_count = (
            "static_cast<uint32_t>(constants->length())"
            if features.constant_pool_length_type == "int"
            else "constants->length().value()"
        )
        checks = {
            "exactly_five_files_changed": changed
            == sorted((D8_CC, D8_H, PRINTER_CC, STRING_CC, SERIALIZER_CC)),
            "loader_registered": (
                PATCH_MARKER in d8 and 'global_template->Set(isolate, "loadjsc"' in d8
            ),
            "direct_handle_api_used": all(
                token in d8
                for token in (
                    "i::MaybeDirectHandle<i::SharedFunctionInfo>",
                    "i::DirectHandle<i::SharedFunctionInfo>",
                    "i::ScriptDetails script_details",
                )
            ),
            "trusted_constant_pool_inferred": (
                "auto constants = bytecode->constant_pool();" in d8
                and "i::Tagged<i::FixedArray> constants" not in d8
            ),
            "constant_pool_length_api_used": (
                f"const uint32_t constant_count = {expected_constant_count};" in d8
                and "index < constants->length()" not in d8
            ),
            "owned_vector_reader_used": "base::OwnedVector<char> file_data" in d8,
            "strict_cache_preflight_before_magic_normalization": all(
                token in d8
                for token in (
                    "i::SerializedCodeData::kHeaderSize",
                    "i::SerializedCodeData::kPayloadLengthOffset",
                    "base::ReadLittleEndianValue<uint32_t>",
                    "payload_length != expected_payload_length",
                    "original_magic & ~kEmbedderMagicBits",
                )
            )
            and d8.index("payload_length != expected_payload_length")
            < d8.index("JSC2JS_EMBEDDER_MAGIC_NORMALIZATION"),
            "gc_rooted_flat_non_recursive_worklist": all(
                token in d8
                for token in (
                    "i::DirectHandleVector<i::SharedFunctionInfo> pending(isolate)",
                    "i::DirectHandleVector<i::SharedFunctionInfo> printed(isolate)",
                    "previous.is_identical_to(current)",
                    "pending.emplace_back(i::Cast<i::SharedFunctionInfo>(object), isolate)",
                )
            )
            and "std::vector<i::DirectHandle<i::SharedFunctionInfo>>" not in d8,
            "cross_embedder_identity_checks_bypassed": (
                "JSC2JS_EMBEDDER_MAGIC_NORMALIZATION" in d8
                and all(
                    marker in serializer
                    for marker in (
                        "JSC2JS_SOURCE_HASH_BYPASS",
                        "JSC2JS_VERSION_HASH_BYPASS",
                        "JSC2JS_FLAGS_HASH_BYPASS",
                        "JSC2JS_READ_ONLY_SNAPSHOT_CHECKSUM_BYPASS",
                    )
                )
            ),
            "magic_normalization_uses_upstream_local_constant": all(
                token in d8
                for token in (
                    "i::SerializedData::kMagicNumberOffset == 0",
                    "i::SerializedData::kMagicNumber",
                    "base::WriteLittleEndianValue(",
                )
            )
            and '"src/snapshot/snapshot-data.h"'
            in sources[SERIALIZER_H],
            "magic_layout_dependencies_byte_identical": all(
                transformed[path] == sources[path]
                for path in (BASE_MEMORY_H, SNAPSHOT_DATA_H)
            ),
            "protected_cache_checks_byte_preserved": protected_token_counts_unchanged(
                sources[SERIALIZER_CC], serializer
            ),
            "only_read_only_snapshot_mismatch_check_removed": (
                sources[SERIALIZER_CC].count(
                    "kReadOnlySnapshotChecksumMismatch"
                )
                == serializer.count("kReadOnlySnapshotChecksumMismatch") + 1
            ),
            "deserializers_byte_identical": all(
                transformed[path] == sources[path]
                for path in (DESERIALIZER_CC, OBJECT_DESERIALIZER_CC)
            ),
            "only_missing_source_print_disabled": (
                transformed[PRINTER_CC]
                == sources[PRINTER_CC].replace(
                    "  PrintSourceCode(os);",
                    "  // JSC2JS_SOURCE_PRINT_BYPASS: source text is absent from .jsc.",
                    1,
                )
            ),
            "string_truncation_only_printer_edit": (
                "JSC2JS_FULL_STRING_PRINT" in transformed[STRING_CC]
            ),
        }
        if not all(checks.values()):
            raise RuntimeError(f"post-transform checks failed: {checks}")
        return {
            "version": version,
            "status": "ok",
            "family": features.family_name,
            "changed_files": changed,
            "upstream_checks_present": upstream_protections(
                sources[SERIALIZER_CC]
            ),
            "checks": checks,
        }
    except Exception as error:
        return {"version": version, "status": "failed", "error": str(error)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--versions",
        type=Path,
        default=Path("compat/modern-v8-versions.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/audit/modern-v8-patch-validation.json"),
    )
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "jsc2js-v8-source-audit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    versions = json.loads(args.versions.read_text(encoding="utf-8"))
    versions = sorted(set(versions), key=version_key)
    cache = RawSourceCache(args.cache_dir)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        records = list(
            executor.map(lambda version: validate_version(cache, version), versions)
        )
    records.sort(key=lambda item: version_key(item["version"]))
    failed = [record for record in records if record["status"] != "ok"]
    payload = {
        "scope": {"first": versions[0], "last": versions[-1]},
        "summary": {
            "versions": len(records),
            "passed": len(records) - len(failed),
            "failed": len(failed),
            "families": len(
                {
                    record["family"]
                    for record in records
                    if record["status"] == "ok"
                }
            ),
        },
        "safety_invariants": {
            "changed_files_per_version": 5,
            "cross_embedder_identity_checks_bypassed": [
                "external_reference_table_size_magic",
                "source",
                "version",
                "flags",
                "read_only_snapshot",
            ],
            "loader_magic_normalized_to_local_table": True,
            "loader_requires_exact_header_payload_boundary": True,
            "loader_rejects_non_v8_magic_family": True,
            "exact_tag_magic_layout_and_write_api_verified": True,
            "upstream_magic_checks_preserved": True,
            "read_only_snapshot_checksum_preserved": False,
            "header_length_checksum_and_normalized_magic_checked": True,
            "deserializer_protocol_checks_preserved": True,
            "heap_short_print_preserved": True,
            "missing_source_print_disabled": True,
            "nested_functions_use_a_flat_deduplicated_worklist": True,
            "worklist_uses_v8_strong_root_allocator": True,
        },
        "versions": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["summary"], ensure_ascii=False))
    for record in failed:
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
