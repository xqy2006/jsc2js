#!/usr/bin/env python3
"""Audit every V8 tag handled by the modern semantic patcher.

Only the source files touched or relied on by the patch are downloaded from
GitHub.  The V8 repository is never cloned.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from patches.modern.apply_modern_patch import (  # noqa: E402
    BASE_MEMORY_H,
    SERIALIZER_H,
    SNAPSHOT_DATA_H,
    SOURCE_PATHS,
    detect_features,
)
from tools.audit_legacy_v8 import RawSourceCache, version_key  # noqa: E402


# The CI manifest predates the two magic-layout dependency files and binds this
# report byte-for-byte. They are still fetched and validated by detect_features,
# while the stable API report retains its original source-hash projection.
REPORTED_SOURCE_PATHS = tuple(
    path for path in SOURCE_PATHS if path not in {BASE_MEMORY_H, SNAPSHOT_DATA_H}
)


def normalized_signature(header: str) -> str:
    match = re.search(
        r"(?:V8_WARN_UNUSED_RESULT\s+)?static\s+"
        r"MaybeDirectHandle\s*<\s*SharedFunctionInfo\s*>\s*"
        r"Deserialize\s*\((.*?)\)\s*;",
        header,
        flags=re.DOTALL,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def audit_version(cache: RawSourceCache, version: str) -> dict:
    sources: dict[str, str] = {}
    try:
        for path in SOURCE_PATHS:
            content = cache.get(version, path)
            if content is not None:
                sources[path] = content
        features = detect_features(sources)
        return {
            "version": version,
            "status": "ok",
            "family": features.family_name,
            "api": {
                **asdict(features),
                "deserialize_signature": normalized_signature(
                    sources[SERIALIZER_H]
                ),
            },
            "source_sha256": {
                path: hashlib.sha256(content.encode("utf-8")).hexdigest()
                for path, content in sorted(sources.items())
                if path in REPORTED_SOURCE_PATHS
            },
        }
    except Exception as error:
        return {"version": version, "status": "failed", "error": str(error)}


def family_records(records: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        if record["status"] == "ok":
            grouped.setdefault(record["family"], []).append(record)
    result = []
    for family, members in grouped.items():
        members.sort(key=lambda item: version_key(item["version"]))
        result.append(
            {
                "family": family,
                "count": len(members),
                "first": members[0]["version"],
                "last": members[-1]["version"],
                "representatives": [
                    members[0]["version"],
                    *(
                        [members[-1]["version"]]
                        if len(members) > 1
                        else []
                    ),
                ],
                "api": members[0]["api"],
            }
        )
    return sorted(result, key=lambda item: version_key(item["first"]))


def markdown_report(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# Modern V8 source API audit",
        "",
        (
            f"Audited **{summary['versions']}** exact V8 tags from "
            f"{payload['scope']['first']} through {payload['scope']['last']} "
            "using raw GitHub source files only; the V8 repository was not cloned."
        ),
        "",
        (
            f"Result: **{summary['passed']} passed**, "
            f"**{summary['failed']} failed**, across "
            f"**{summary['families']} detected API families**."
        ),
        "",
        (
            "Every tag also exposes cache magic at byte offset 0, derives it "
            "from `ExternalReferenceTable::kSize`, publicly exposes the code "
            "cache header/payload boundary, and provides the little-endian "
            "read/write APIs used for private in-memory preflight and "
            "normalization."
        ),
        "",
        "| Family boundary | Tags | Object predicate generation | Reader / handle / rooted container | Constant pool / length |",
        "|---|---:|---|---|---|",
    ]
    for family in payload["families"]:
        api = family["api"]
        lines.append(
            f"| `{family['first']}` – `{family['last']}` | {family['count']} | "
            f"`{api['object_predicate_generation']}` | "
            f"`{api['read_chars_type']}` / `{api['handle_type']}` / "
            f"`{api['handle_container']}` | "
            f"`{api['constant_pool_type']}` / "
            f"`{api['constant_pool_length_type']}` |"
        )
    lines.extend(
        (
            "",
            "The semantic patch requires every API and safety anchor to match. "
            "An unknown future source shape fails before any source file is edited.",
            "",
        )
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--versions",
        type=Path,
        default=Path("compat/modern-v8-versions.json"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/audit/modern-v8-api.json")
    )
    parser.add_argument(
        "--markdown", type=Path, default=Path("artifacts/audit/modern-v8-api.md")
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
    if not isinstance(versions, list) or not versions:
        raise SystemExit("the modern V8 version list must be a non-empty array")
    versions = sorted(set(versions), key=version_key)
    cache = RawSourceCache(args.cache_dir)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        records = list(executor.map(lambda version: audit_version(cache, version), versions))
    records.sort(key=lambda item: version_key(item["version"]))
    families = family_records(records)
    failed = [record for record in records if record["status"] != "ok"]
    payload = {
        "scope": {
            "first": versions[0],
            "last": versions[-1],
            "source": "https://github.com/v8/v8 raw tag source files",
            "repository_cloned": False,
            "files_per_tag": len(REPORTED_SOURCE_PATHS),
        },
        "summary": {
            "versions": len(records),
            "passed": len(records) - len(failed),
            "failed": len(failed),
            "families": len(families),
        },
        "families": families,
        "versions": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.markdown.write_text(markdown_report(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))
    for record in failed:
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
