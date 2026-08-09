#!/usr/bin/env python3
"""Audit host-build templates selected by every supported legacy V8 tag."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from build_versions_batch_v3 import WINDOWS_TOOLCHAIN_ARGS_RE  # noqa: E402
from tools.audit_legacy_v8 import RawSourceCache, version_key  # noqa: E402


BUILD_ROOT = "https://chromium.googlesource.com/chromium/src/build"
BUILD_PATH = "toolchain/win/setup_toolchain.py"


def extract_build_revision(deps: str) -> str:
    """Return the chromium/src/build revision from a V8 DEPS file."""
    marker = "chromium/src/build.git"
    position = deps.find(marker)
    if position < 0:
        raise ValueError("chromium/src/build.git dependency was not found")
    match = re.search(r"[0-9a-f]{40}", deps[position + len(marker) : position + 400])
    if not match:
        raise ValueError("chromium/src/build revision was not found")
    return match.group(0)


class BuildSourceCache:
    def __init__(self, directory: Path, retries: int = 5):
        self.directory = directory
        self.retries = retries
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, revision: str, source_path: str) -> Path:
        digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:16]
        return self.directory / revision / f"{digest}.txt"

    def get(self, revision: str, source_path: str) -> str:
        key = (revision, source_path)
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            return self._get_locked(revision, source_path)

    def _get_locked(self, revision: str, source_path: str) -> str:
        cached = self._path(revision, source_path)
        if cached.exists():
            return cached.read_text(encoding="utf-8", errors="replace")
        url = f"{BUILD_ROOT}/+/{revision}/{source_path}?format=TEXT"
        for attempt in range(self.retries):
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "jsc2js-v8-host-audit"}
                )
                with urllib.request.urlopen(request, timeout=45) as response:
                    encoded = response.read()
                content = base64.b64decode(encoded).decode("utf-8", errors="replace")
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(content, encoding="utf-8")
                return content
            except urllib.error.HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504}:
                    raise
            except (TimeoutError, urllib.error.URLError):
                pass
            time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"failed to fetch {url}")


def normalize_template(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def classify_version(
    v8_cache: RawSourceCache, build_cache: BuildSourceCache, version: str
) -> dict:
    try:
        deps = v8_cache.get(version, "DEPS")
        if deps is None:
            raise RuntimeError("V8 DEPS file was not found")
        revision = extract_build_revision(deps)
        setup = build_cache.get(revision, BUILD_PATH)
        matches = list(WINDOWS_TOOLCHAIN_ARGS_RE.finditer(setup))
        template = normalize_template(matches[0].group(0)) if len(matches) == 1 else ""
        requires_v142 = version_key(version) < version_key("9.0.0")
        compatible = len(matches) == 1
        status = "ok" if compatible else "incompatible"
        return {
            "version": version,
            "status": status,
            "build_revision": revision,
            "setup_toolchain_path": BUILD_PATH,
            "vcvars_args_matches": len(matches),
            "vcvars_args_template": template,
            "requires_v142_compatibility": requires_v142,
            "v142_injection_supported": compatible if requires_v142 else None,
        }
    except Exception as error:
        return {"version": version, "status": "fetch-error", "error": str(error)}


def write_markdown(path: Path, payload: dict) -> None:
    summary = payload["summary"]
    lines = [
        "# Legacy V8 host-tool audit",
        "",
        f"Exact tags audited: **{summary['versions']}**",
        "",
        f"Chromium build revisions: **{summary['build_revisions']}**",
        "",
        f"Windows toolchain templates: **{summary['templates']}**",
        "",
        "| Template | First V8 | Last V8 | Tags | v142 tags |",
        "|---|---:|---:|---:|---:|",
    ]
    for family in payload["families"]:
        template = family["template"].replace("|", "\\|")
        lines.append(
            f"| `{template}` | {family['first']} | {family['last']} | "
            f"{family['count']} | {family['v142_count']} |"
        )
    lines.extend(
        [
            "",
            "For V8 before 9.0, every exact tag must match exactly one `vcvarsall` "
            "argument template so the installed MSVC v142 toolset can be selected.",
            "The JSON report records the exact V8 tag, Chromium build revision, "
            "template, and compatibility result.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, default=Path("audit/legacy-v8-api.json"))
    parser.add_argument(
        "--output", type=Path, default=Path("audit/legacy-v8-host-tools.json")
    )
    parser.add_argument(
        "--markdown", type=Path, default=Path("audit/legacy-v8-host-tools.md")
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
    api_audit = json.loads(args.audit.read_text(encoding="utf-8"))
    versions = [record["version"] for record in api_audit["versions"]]
    v8_cache = RawSourceCache(args.v8_cache_dir)
    build_cache = BuildSourceCache(args.build_cache_dir)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(
            executor.map(
                lambda version: classify_version(v8_cache, build_cache, version),
                versions,
            )
        )
    results.sort(key=lambda item: version_key(item["version"]))
    failures = [item for item in results if item["status"] != "ok"]

    families = []
    by_template: dict[str, list[dict]] = {}
    for result in results:
        if result["status"] == "ok":
            by_template.setdefault(result["vcvars_args_template"], []).append(result)
    for template, records in sorted(
        by_template.items(), key=lambda item: version_key(item[1][0]["version"])
    ):
        families.append(
            {
                "family": hashlib.sha256(template.encode("utf-8")).hexdigest()[:12],
                "template": template,
                "first": records[0]["version"],
                "last": records[-1]["version"],
                "count": len(records),
                "v142_count": sum(
                    bool(record["requires_v142_compatibility"]) for record in records
                ),
            }
        )

    payload = {
        "scope": api_audit["scope"],
        "summary": {
            "versions": len(results),
            "ok": len(results) - len(failures),
            "incompatible": sum(
                result["status"] == "incompatible" for result in results
            ),
            "fetch_errors": sum(
                result["status"] == "fetch-error" for result in results
            ),
            "build_revisions": len(
                {
                    result.get("build_revision")
                    for result in results
                    if result.get("build_revision")
                }
            ),
            "templates": len(families),
            "v142_versions": sum(
                bool(result.get("requires_v142_compatibility")) for result in results
            ),
        },
        "families": families,
        "versions": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_markdown(args.markdown, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False))
    for failure in failures[:20]:
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
