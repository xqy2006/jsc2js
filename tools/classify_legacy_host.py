#!/usr/bin/env python3
"""Classify host requirements for a bounded batch of exact V8 tags."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from build_versions_batch_v3 import (  # noqa: E402
    windows_compatibility_year,
    windows_legacy_toolset_spec,
)


VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?\Z")


def parse_versions(raw: str, maximum: int = 5) -> list[str]:
    try:
        versions = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("versions_json is not valid JSON") from error
    if not isinstance(versions, list) or not 1 <= len(versions) <= maximum:
        raise ValueError(f"versions_json must contain 1-{maximum} tags")
    if any(not isinstance(version, str) or not VERSION_RE.fullmatch(version) for version in versions):
        raise ValueError("versions_json contains an invalid V8 tag")
    if len(set(versions)) != len(versions):
        raise ValueError("versions_json must not contain duplicate tags")
    return versions


def classify_versions(versions: list[str]) -> dict[str, bool | str]:
    majors = [int(version.split(".", 1)[0]) for version in versions]
    toolsets = {
        spec[1]
        for version in versions
        if (spec := windows_legacy_toolset_spec(version)) is not None
    }
    return {
        "python2": any(major < 9 for major in majors),
        "v141": "v141" in toolsets,
        "v142": "v142" in toolsets,
        "vs_year": windows_compatibility_year(versions[0]),
    }


def write_github_output(path: Path, values: dict[str, bool | str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            rendered = str(value).lower() if isinstance(value, bool) else value
            output.write(f"{key}={rendered}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--versions-json", required=True)
    parser.add_argument("--expected-first")
    parser.add_argument("--max-versions", type=int, default=5)
    parser.add_argument(
        "--github-output",
        type=Path,
        default=Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_versions < 1:
        raise SystemExit("max-versions must be positive")
    try:
        versions = parse_versions(args.versions_json, args.max_versions)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if args.expected_first and versions[0] != args.expected_first:
        raise SystemExit("the first versions_json tag must equal expected-first")
    values = classify_versions(versions)
    if args.github_output:
        write_github_output(args.github_output, values)
    print(json.dumps({"versions": versions, **values}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
