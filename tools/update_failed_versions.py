#!/usr/bin/env python3
"""Normalize and update the failed V8 version tracking file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")


def version_key(version: str) -> tuple[int, int, int, int]:
    parts = tuple(int(part) for part in version.split("."))
    return (parts + (0, 0, 0, 0))[:4]


def normalized_versions(values) -> set[str]:
    result = set()
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if SEMVER_RE.fullmatch(value):
            result.add(value)
    return result


def update_failed_versions(
    path: Path, *, add_files: list[Path], remove: list[str]
) -> list[str]:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []
    versions = normalized_versions(existing if isinstance(existing, list) else [])
    for add_file in add_files:
        if add_file.is_file():
            versions.update(
                normalized_versions(add_file.read_text(encoding="utf-8").splitlines())
            )
    versions.difference_update(normalized_versions(remove))
    ordered = sorted(versions, key=version_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(ordered, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return ordered


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--failed-json", type=Path, required=True)
    parser.add_argument("--add-file", type=Path, action="append", default=[])
    parser.add_argument("--remove", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    versions = update_failed_versions(
        args.failed_json, add_files=args.add_file, remove=args.remove
    )
    print(f"failed versions: {len(versions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
