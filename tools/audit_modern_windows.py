#!/usr/bin/env python3
"""Audit exact Chromium Windows SDK pins for modern V8 tags."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.audit_legacy_host_tools import (  # noqa: E402
    BuildSourceCache,
    extract_build_revision,
)
from tools.audit_legacy_v8 import RawSourceCache, version_key  # noqa: E402


SETUP_TOOLCHAIN = "toolchain/win/setup_toolchain.py"
VS_TOOLCHAIN = "vs_toolchain.py"
RUNNER_NATIVE_SDK = "10.0.26100.0"
SDK_INSTALLER = "tools/install_windows_sdk.ps1"
SDK_INSTALLER_URL = "https://go.microsoft.com/fwlink/?linkid=2372508"
WORKFLOWS = (
    ".github/workflows/compile.yml",
    ".github/workflows/main.yml",
    ".github/workflows/update_worker.yml",
)


def extract_assignment(source: str, name: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]", source
    )
    if not match:
        raise ValueError(f"{name} assignment is missing")
    return match.group(1)


def classify_version(
    v8_cache: RawSourceCache, build_cache: BuildSourceCache, version: str
) -> dict:
    try:
        deps = v8_cache.get(version, "DEPS")
        if deps is None:
            raise ValueError("V8 DEPS is missing")
        build_revision = extract_build_revision(deps)
        setup = build_cache.get(build_revision, SETUP_TOOLCHAIN)
        vs_toolchain = build_cache.get(build_revision, VS_TOOLCHAIN)
        setup_sdk = extract_assignment(setup, "SDK_VERSION")
        vs_sdk = extract_assignment(vs_toolchain, "SDK_VERSION")
        if setup_sdk != vs_sdk:
            raise ValueError(
                f"SDK pin mismatch: setup={setup_sdk} vs_toolchain={vs_sdk}"
            )
        return {
            "version": version,
            "status": "ok",
            "build_revision": build_revision,
            "windows_sdk": setup_sdk,
            "toolchain_hash": extract_assignment(vs_toolchain, "TOOLCHAIN_HASH"),
            "source_sha256": {
                SETUP_TOOLCHAIN: hashlib.sha256(setup.encode()).hexdigest(),
                VS_TOOLCHAIN: hashlib.sha256(vs_toolchain.encode()).hexdigest(),
            },
        }
    except Exception as error:
        return {"version": version, "status": "failed", "error": str(error)}


def sdk_ranges(records: list[dict]) -> list[dict]:
    ranges: list[dict] = []
    for record in records:
        if record["status"] != "ok":
            continue
        sdk = record["windows_sdk"]
        if not ranges or ranges[-1]["windows_sdk"] != sdk:
            ranges.append(
                {
                    "first": record["version"],
                    "last": record["version"],
                    "count": 1,
                    "windows_sdk": sdk,
                }
            )
        else:
            ranges[-1]["last"] = record["version"]
            ranges[-1]["count"] += 1
    return ranges


def workflow_coverage(required_sdks: set[str]) -> dict[str, dict[str, bool]]:
    non_native_sdks = required_sdks - {RUNNER_NATIVE_SDK}
    installer = (REPO_ROOT / SDK_INSTALLER).read_text(encoding="utf-8")
    installer_support = {
        sdk: (
            sdk in installer
            and SDK_INSTALLER_URL in installer
            and "Get-AuthenticodeSignature" in installer
        )
        for sdk in non_native_sdks
    }
    return {
        workflow: {
            sdk: (
                installer_support[sdk]
                and SDK_INSTALLER
                in (REPO_ROOT / workflow).read_text(encoding="utf-8")
            )
            for sdk in sorted(non_native_sdks)
        }
        for workflow in WORKFLOWS
    }


def markdown_report(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# Modern V8 Windows SDK audit",
        "",
        (
            f"Audited **{summary['versions']}** exact modern V8 tags and "
            f"**{summary['build_revisions']}** Chromium build revisions."
        ),
        "",
        (
            f"Result: **{summary['passed']} passed**, "
            f"**{summary['failed']} failed**."
        ),
        "",
        "| V8 range | Tags | Exact Chromium SDK pin | Runner handling |",
        "|---|---:|---|---|",
    ]
    for item in payload["sdk_ranges"]:
        handling = (
            "native"
            if item["windows_sdk"] == RUNNER_NATIVE_SDK
            else "official Microsoft SDK installed on demand"
        )
        lines.append(
            f"| `{item['first']}` – `{item['last']}` | {item['count']} | "
            f"`{item['windows_sdk']}` | {handling} |"
        )
    lines.extend(
        [
            "",
            "Both `setup_toolchain.py` and `vs_toolchain.py` must carry the "
            "same SDK pin for every exact build revision. Non-native SDK pins "
            "must be supported by the signed installer helper and invoked by "
            "compile, production, and rebuild workflows.",
            "",
            "The 10.0.28000 installer is pinned from the "
            "[Microsoft Windows SDK download page]"
            "(https://learn.microsoft.com/en-us/windows/apps/windows-sdk/downloads).",
            "",
            "Only individual immutable V8 and Chromium build files were read; "
            "neither repository was cloned.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--versions", type=Path, default=Path("compat/modern-v8-versions.json")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/audit/modern-v8-windows.json"),
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path("artifacts/audit/modern-v8-windows.md"),
    )
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument(
        "--v8-cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "jsc2js-v8-source-audit",
    )
    parser.add_argument(
        "--build-cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "jsc2js-v8-host-audit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    versions = sorted(
        set(json.loads(args.versions.read_text(encoding="utf-8"))), key=version_key
    )
    v8_cache = RawSourceCache(args.v8_cache_dir)
    build_cache = BuildSourceCache(args.build_cache_dir)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        records = list(
            executor.map(
                lambda version: classify_version(v8_cache, build_cache, version),
                versions,
            )
        )
    records.sort(key=lambda item: version_key(item["version"]))
    failed = [record for record in records if record["status"] != "ok"]
    required_sdks = {
        record["windows_sdk"] for record in records if record["status"] == "ok"
    }
    coverage = workflow_coverage(required_sdks)
    uncovered = [
        f"{workflow}:{sdk}"
        for workflow, sdks in coverage.items()
        for sdk, present in sdks.items()
        if not present
    ]
    payload = {
        "scope": {
            "first": versions[0],
            "last": versions[-1],
            "runner_native_sdk": RUNNER_NATIVE_SDK,
            "sdk_installer": SDK_INSTALLER,
            "sdk_installer_url": SDK_INSTALLER_URL,
            "repository_cloned": False,
        },
        "summary": {
            "versions": len(records),
            "passed": len(records) - len(failed),
            "failed": len(failed),
            "build_revisions": len(
                {record["build_revision"] for record in records if "build_revision" in record}
            ),
            "sdk_pins": sorted(required_sdks),
            "uncovered_workflow_installers": uncovered,
        },
        "sdk_ranges": sdk_ranges(records),
        "workflow_install_coverage": coverage,
        "versions": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    args.markdown.write_text(markdown_report(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))
    for record in failed:
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
    if uncovered:
        print(
            f"uncovered workflow installers: {', '.join(uncovered)}",
            file=sys.stderr,
        )
    return 0 if not failed and not uncovered else 1


if __name__ == "__main__":
    raise SystemExit(main())
