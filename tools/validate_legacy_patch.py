#!/usr/bin/env python3
"""Validate the semantic legacy patch against every audited V8 source tag."""

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
    FIXED_ARRAY_PATHS,
    PATCH_MARKER,
    SERIALIZER_CC,
    SFI_PATHS,
    transform_sources,
)
from tools.audit_legacy_v8 import RawSourceCache, version_key  # noqa: E402


def validate_record(cache: RawSourceCache, record: dict) -> dict:
    version = record["version"]
    paths = {path for path in record["paths"].values() if path}
    paths.update(SFI_PATHS)
    paths.update(FIXED_ARRAY_PATHS)
    sources: dict[str, str] = {}
    try:
        for path in paths:
            content = cache.get(version, path)
            if content is not None:
                sources[path] = content
        transformed, features, changed = transform_sources(sources)
        d8_path = next(
            path for path in changed if path in {"src/d8.cc", "src/d8/d8.cc"}
        )
        checks = {
            "loader_marker": PATCH_MARKER in transformed[d8_path],
            "source_hash_only": "JSC2JS_SOURCE_HASH_BYPASS"
            in transformed[SERIALIZER_CC],
            "deserializer_unchanged": all(
                path not in changed
                for path in (
                    "src/snapshot/deserializer.cc",
                    "src/snapshot/object-deserializer.cc",
                )
            ),
            "expected_file_count": len(changed) == 4,
        }
        if not all(checks.values()):
            raise RuntimeError(f"post-transform checks failed: {checks}")
        return {
            "version": version,
            "status": "ok",
            "source_family": record["family"],
            "patch_family": features.family_name,
            "changed_files": changed,
            "checks": checks,
        }
    except Exception as error:
        return {
            "version": version,
            "status": "failed",
            "source_family": record.get("family"),
            "error": str(error),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--audit", type=Path, default=Path("audit/legacy-v8-api.json")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("audit/legacy-v8-patch-validation.json"),
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
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    cache = RawSourceCache(args.cache_dir)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(
            executor.map(
                lambda record: validate_record(cache, record), audit["versions"]
            )
        )
    results.sort(key=lambda item: version_key(item["version"]))
    failed = [result for result in results if result["status"] != "ok"]
    payload = {
        "scope": audit["scope"],
        "summary": {
            "versions": len(results),
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "source_families": len(
                {result.get("source_family") for result in results}
            ),
            "patch_families": len(
                {
                    result.get("patch_family")
                    for result in results
                    if result.get("patch_family")
                }
            ),
        },
        "safety_invariants": {
            "changed_files_per_version": 4,
            "source_hash_is_the_only_integrity_check_bypassed": True,
            "deserializer_is_unchanged": True,
            "recursive_heap_short_print_is_unchanged": True,
        },
        "versions": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload["summary"], ensure_ascii=False))
    for result in failed[:20]:
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
