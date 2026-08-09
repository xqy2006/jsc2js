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

from build_versions_batch_v3 import (  # noqa: E402
    WINDOWS_TOOLCHAIN_ARGS_RE,
    WINDOWS_TOOLCHAIN_ENV_RE,
    uses_in_tree_gyp,
    windows_legacy_toolset_spec,
)
from tools.audit_legacy_v8 import RawSourceCache, version_key  # noqa: E402


BUILD_ROOT = "https://chromium.googlesource.com/chromium/src/build"
BUILD_PATH = "toolchain/win/setup_toolchain.py"
LEGACY_VCVARS_PATH_RE = re.compile(
    r"['\"]VC[/\\]vcvarsall\.bat['\"]|"
    r"['\"]VC['\"]\s*,\s*['\"]vcvarsall\.bat['\"]"
)


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


def classify_linux_host_mode(version: str, deps: str) -> str:
    """Describe how CI supplies a usable compiler/sysroot for this tag."""
    if uses_in_tree_gyp(version):
        return "hosted-clang-in-tree-gyp"
    major, minor = (int(part) for part in version.split(".", 2)[:2])
    if (major, minor) == (5, 2):
        return "hosted-clang-without-sysroot-hook"
    if "install-sysroot.py" in deps:
        return "pinned-clang-with-sysroot-hook"
    return "pinned-clang-without-v8-sysroot-hook"


def classify_version(
    v8_cache: RawSourceCache, build_cache: BuildSourceCache, version: str
) -> dict:
    try:
        deps = v8_cache.get(version, "DEPS")
        if deps is None:
            raise RuntimeError("V8 DEPS file was not found")
        toolset_spec = windows_legacy_toolset_spec(version)
        required_toolset = toolset_spec[1] if toolset_spec else "current"
        linux_host_mode = classify_linux_host_mode(version, deps)
        deps_has_sysroot_hook = "install-sysroot.py" in deps
        if uses_in_tree_gyp(version):
            if "chromium/src/build.git" in deps:
                raise RuntimeError("V8 5.1 unexpectedly declares an external build repo")
            gyp_v8 = v8_cache.get(version, "build/gyp_v8") or ""
            vs_toolchain = v8_cache.get(version, "build/vs_toolchain.py") or ""
            makefile = v8_cache.get(version, "Makefile") or ""
            checks = {
                "ninja_generator": "GYP_GENERATORS" in gyp_v8,
                "gyp_defines": "GYP_DEFINES" in gyp_v8,
                "vs2015_compatibility": (
                    "GYP_MSVS_VERSION" in vs_toolchain
                    and "elif os.environ['GYP_MSVS_VERSION'] == '2015':"
                    in vs_toolchain
                ),
                "vs_override": "GYP_MSVS_OVERRIDE_PATH" in vs_toolchain,
                "sdk_environment": "WINDOWSSDKDIR" in vs_toolchain,
                "object_print": "v8_object_print=1" in makefile,
                "disassembler": "v8_enable_disassembler=1" in makefile,
            }
            compatible = all(checks.values())
            return {
                "version": version,
                "status": "ok" if compatible else "incompatible",
                "generator_style": "in-tree-gyp",
                "build_revision": None,
                "setup_toolchain_path": "build/gyp_v8 + build/vs_toolchain.py",
                "vcvars_args_matches": 0,
                "vcvars_environment_matches": 0,
                "vcvars_args_template": "in-tree GYP/Ninja with imported vcvarsall environment",
                "required_toolset": required_toolset,
                "legacy_vcvars_reference_present": True,
                "legacy_vcvars_entry_point_provided": True,
                "toolset_injection_supported": checks["vs2015_compatibility"],
                "installed_sdk_injection_supported": checks["sdk_environment"],
                "linux_host_mode": linux_host_mode,
                "v8_deps_has_sysroot_hook": deps_has_sysroot_hook,
                "in_tree_gyp_checks": checks,
            }
        revision = extract_build_revision(deps)
        setup = build_cache.get(revision, BUILD_PATH)
        matches = list(WINDOWS_TOOLCHAIN_ARGS_RE.finditer(setup))
        environment_matches = list(WINDOWS_TOOLCHAIN_ENV_RE.finditer(setup))
        template = normalize_template(matches[0].group(0)) if len(matches) == 1 else ""
        legacy_vcvars_reference = bool(LEGACY_VCVARS_PATH_RE.search(setup))
        compatible = len(matches) == 1 and len(environment_matches) == 1
        status = "ok" if compatible else "incompatible"
        return {
            "version": version,
            "status": status,
            "generator_style": "external-gn",
            "build_revision": revision,
            "setup_toolchain_path": BUILD_PATH,
            "vcvars_args_matches": len(matches),
            "vcvars_environment_matches": len(environment_matches),
            "vcvars_args_template": template,
            "required_toolset": required_toolset,
            "legacy_vcvars_reference_present": legacy_vcvars_reference,
            "legacy_vcvars_entry_point_provided": bool(
                toolset_spec and legacy_vcvars_reference
            ),
            "toolset_injection_supported": compatible,
            "installed_sdk_injection_supported": len(environment_matches) == 1,
            "linux_host_mode": linux_host_mode,
            "v8_deps_has_sysroot_hook": deps_has_sysroot_hook,
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
        f"CI legacy `VC/vcvarsall.bat` bridge tags: "
        f"**{summary['legacy_vcvars_entry_point_tags']}**",
        "",
        "Linux host modes: "
        + ", ".join(
            f"`{mode}` **{count}**"
            for mode, count in summary["linux_host_modes"].items()
        ),
        "",
        "| Template | First V8 | Last V8 | Tags | Toolsets |",
        "|---|---:|---:|---:|---|",
    ]
    for family in payload["families"]:
        template = family["template"].replace("|", "\\|")
        lines.append(
            f"| `{template}` | {family['first']} | {family['last']} | "
            f"{family['count']} | {family['toolsets']} |"
        )
    lines.extend(
        [
        "",
        "V8 5.1 is audited against its in-tree GYP/Ninja generator and imports "
        "the selected hosted `vcvarsall` environment directly. V8 5.2 predates "
        "the Linux sysroot hook, so CI disables the missing Wheezy sysroot and "
        "routes the pinned clang paths to the hosted compiler. Every later exact "
        "tag must match one `vcvarsall` argument template and one environment-"
        "capture call, so CI can select both the historical MSVC headers and "
        "the SDK version actually installed on the runner. "
        "The build selects v142 for V8 5.x and 8.x–9.x, v141 for "
            "6.x–7.x, and the current toolset for 10.x–11.x. For every tag "
            "where a historical toolset is selected and the pinned setup "
            "script retains the legacy path, CI provides a forwarding "
            "`VC/vcvarsall.bat` entry point.",
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
                "toolsets": ", ".join(
                    sorted({record["required_toolset"] for record in records})
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
            "toolset_counts": {
                toolset: sum(
                    result.get("required_toolset") == toolset for result in results
                )
                for toolset in ("v140", "v141", "v142", "current")
            },
            "legacy_vcvars_entry_point_tags": sum(
                bool(result.get("legacy_vcvars_entry_point_provided"))
                for result in results
            ),
            "linux_host_modes": {
                mode: sum(result.get("linux_host_mode") == mode for result in results)
                for mode in sorted(
                    {
                        result.get("linux_host_mode")
                        for result in results
                        if result.get("linux_host_mode")
                    }
                )
            },
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
