#!/usr/bin/env python3
"""Audit host-build templates selected by every supported legacy V8 tag."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import tokenize
import urllib.error
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from build_versions_batch_v3 import (  # noqa: E402
    WINDOWS_ATLMFC_MARKER,
    WINDOWS_SDK_MARKER,
    WINDOWS_TOOLCHAIN_ARGS_RE,
    WINDOWS_TOOLCHAIN_ENV_RE,
    WINDOWS_UM_LIB_MARKER,
    WINDOWS_VS_VERSION_FUNCTION_RE,
    WINDOWS_VS_VERSION_MARKER,
    WINDOWS_VCVARS_MARKER,
    patch_windows_setup_toolchain_source,
    patch_windows_vs_toolchain_source,
    uses_in_tree_gyp,
    windows_compatibility_year,
    windows_legacy_toolset_spec,
)
from tools.audit_legacy_v8 import RawSourceCache, version_key  # noqa: E402


BUILD_ROOT = "https://chromium.googlesource.com/chromium/src/build"
BUILD_PATH = "toolchain/win/setup_toolchain.py"
VS_TOOLCHAIN_PATH = "vs_toolchain.py"
COMPILER_BUILD_PATH = "config/compiler/BUILD.gn"
COMPILER_GNI_PATH = "config/compiler/compiler.gni"
GCC_TOOLCHAIN_PATH = "toolchain/gcc_toolchain.gni"
CLANG_ROOT = "https://chromium.googlesource.com/chromium/src/tools/clang.git"
CLANG_PATH = "scripts/update.py"
LEGACY_VCVARS_PATH_RE = re.compile(
    r"['\"]VC[/\\]vcvarsall\.bat['\"]|"
    r"['\"]VC['\"]\s*,\s*['\"]vcvarsall\.bat['\"]"
)


def extract_dependency_revision(deps: str, marker: str) -> str:
    """Return the exact revision following a repository marker in DEPS."""
    position = deps.find(marker)
    if position < 0:
        raise ValueError(f"{marker} dependency was not found")
    match = re.search(r"[0-9a-f]{40}", deps[position + len(marker) : position + 400])
    if not match:
        raise ValueError(f"{marker} revision was not found")
    return match.group(0)


def extract_build_revision(deps: str) -> str:
    """Return the chromium/src/build revision from a V8 DEPS file."""
    return extract_dependency_revision(deps, "chromium/src/build.git")


def extract_clang_revision(deps: str) -> str:
    """Return the chromium/src/tools/clang revision from a V8 DEPS file."""
    return extract_dependency_revision(deps, "chromium/src/tools/clang.git")


class BuildSourceCache:
    def __init__(self, directory: Path, root: str = BUILD_ROOT, retries: int = 5):
        self.directory = directory
        self.root = root.rstrip("/")
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
        url = f"{self.root}/+/{revision}/{source_path}?format=TEXT"
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


def extract_vs_toolchain_years(source: str) -> list[str]:
    """Return VS year keys accepted by an exact Chromium build checkout."""
    return sorted(
        set(
            re.findall(
                r"(?m)^\s*\(?['\"](20(?:13|15|17|19|22))['\"]\s*(?:[:,]|\))",
                source,
            )
        )
    )


def extract_dia_dll_years(source: str) -> list[str]:
    """Return the VS year keys used by the clang hook's DIA_DLL lookup."""
    match = re.search(r"(?ms)^\s*DIA_DLL\s*=\s*\{(?P<body>.*?)^\s*\}", source)
    if not match:
        return []
    return sorted(
        set(re.findall(r"['\"](20(?:13|15|17|19|22))['\"]\s*:", match.group("body")))
    )


def clang_hook_uses_keyed_dia_dll(source: str) -> bool:
    """Return whether the hook indexes DIA_DLL with GetVisualStudioVersion()."""
    return bool(re.search(r"DIA_DLL\s*\[\s*msvs_version\s*\]", source))


def extract_clang_release_version(source: str) -> str:
    """Return the clang release advertised by one exact update.py revision."""
    match = re.search(
        r"(?m)^\s*RELEASE_VERSION\s*=\s*['\"](?P<version>[^'\"]+)['\"]",
        source,
    )
    return match.group("version") if match else ""


def clang_supports_selected_toolset(toolset: str, release_version: str) -> bool:
    """Reject the clang 10 + v142 header combination observed on V8 8.0/8.1."""
    if toolset != "v142" or not release_version:
        return True
    return int(release_version.split(".", 1)[0]) >= 11


def audit_setup_toolchain_patch(source: str) -> dict:
    """Replay the production injection and validate its complete Python layout."""
    original_has_atlmfc_assert = "assert vc_lib_atlmfc_path" in source
    um_lib_fallback_anchor_present = "vc_lib_um_path" in source
    try:
        patched = patch_windows_setup_toolchain_source(source)
        idempotent = patch_windows_setup_toolchain_source(patched) == patched
        try:
            list(tokenize.generate_tokens(io.StringIO(patched).readline))
            tokenizable = True
            token_error = ""
        except (IndentationError, SyntaxError, tokenize.TokenError) as error:
            tokenizable = False
            token_error = f"{type(error).__name__}: {error}"
        preserved_output_lines = {
            line.rstrip()
            for line in source.splitlines()
            if "print" in line
            and ("vc_lib_um_path" in line or "libpath_flags" in line)
        }
        patched_lines = {line.rstrip() for line in patched.splitlines()}
        checks = {
            "vcvars_marker_once": patched.count(WINDOWS_VCVARS_MARKER) == 1,
            "sdk_marker_once": patched.count(WINDOWS_SDK_MARKER) == 1,
            "sdk_precedes_vcvars_guard": (
                "args.insert(jsc2js_vcvars_index, jsc2js_sdk_version)" in patched
            ),
            "um_lib_fallback_complete": (
                patched.count(WINDOWS_UM_LIB_MARKER)
                == (1 if um_lib_fallback_anchor_present else 0)
                and (
                    not um_lib_fallback_anchor_present
                    or (
                        "win_sdk_path, 'Lib', jsc2js_sdk_version" in patched
                        and "'um', target_cpu" in patched
                        and "'User32.Lib'" in patched
                    )
                )
            ),
            "unrelated_outputs_preserved": preserved_output_lines <= patched_lines,
            "atlmfc_assertion_complete": (
                "assert vc_lib_atlmfc_path" not in patched
                and patched.count(WINDOWS_ATLMFC_MARKER)
                == (1 if original_has_atlmfc_assert else 0)
            ),
            "idempotent": idempotent,
            "tokenizable": tokenizable,
        }
        return {
            "supported": all(checks.values()),
            "checks": checks,
            "um_lib_fallback_anchor_present": um_lib_fallback_anchor_present,
            "error": token_error,
        }
    except RuntimeError as error:
        return {
            "supported": False,
            "checks": {},
            "um_lib_fallback_anchor_present": um_lib_fallback_anchor_present,
            "error": str(error),
        }


def audit_vs_toolchain_patch(source: str, selected_year: str) -> dict:
    """Replay the hosted VS-version bridge against one exact build helper."""
    function = WINDOWS_VS_VERSION_FUNCTION_RE.search(source)
    bridge_required = bool(function and "GYP_MSVS_VERSION" not in function.group(0))
    try:
        patched = patch_windows_vs_toolchain_source(source)
        idempotent = patch_windows_vs_toolchain_source(patched) == patched
        try:
            list(tokenize.generate_tokens(io.StringIO(patched).readline))
            tokenizable = True
            token_error = ""
        except (IndentationError, SyntaxError, tokenize.TokenError) as error:
            tokenizable = False
            token_error = f"{type(error).__name__}: {error}"

        patched_lines = iter(patched.splitlines())
        original_lines_preserved = all(
            any(candidate == line for candidate in patched_lines)
            for line in source.splitlines()
        )
        checks = {
            "selected_year_supported": selected_year
            in extract_vs_toolchain_years(source),
            "bridge_marker_expected": patched.count(WINDOWS_VS_VERSION_MARKER)
            == (1 if bridge_required else 0),
            "bridge_logic_complete": (
                not bridge_required
                or (
                    "os.environ.get('GYP_MSVS_VERSION')" in patched
                    and "jsc2js_msvs_version in MSVS_VERSIONS" in patched
                    and "return jsc2js_msvs_version" in patched
                )
            ),
            "original_lines_preserved": original_lines_preserved,
            "idempotent": idempotent,
            "tokenizable": tokenizable,
        }
        return {
            "supported": all(checks.values()),
            "bridge_required": bridge_required,
            "checks": checks,
            "error": token_error,
        }
    except RuntimeError as error:
        return {
            "supported": False,
            "bridge_required": bridge_required,
            "checks": {},
            "error": str(error),
        }


def classify_linux_host_mode(version: str, deps: str) -> str:
    """Describe how CI supplies a usable compiler/sysroot for this tag."""
    if uses_in_tree_gyp(version):
        return "hosted-clang-in-tree-gyp"
    major, minor = (int(part) for part in version.split(".", 2)[:2])
    if (major, minor) == (5, 2):
        return "hosted-clang-lld-without-sysroot-hook"
    if "install-sysroot.py" in deps:
        return "pinned-clang-with-sysroot-hook"
    return "pinned-clang-without-v8-sysroot-hook"


def classify_object_print_gn_arg(build_gn: str, v8_gni: str = "") -> str:
    """Return the exact GN object-print argument exposed by a V8 checkout."""
    sources = f"{build_gn}\n{v8_gni}"
    if re.search(r"(?m)^\s*v8_object_print\s*=", sources):
        return "v8_object_print"
    if "v8_enable_object_print" in sources:
        return "v8_enable_object_print"
    return ""


def supports_warning_policy_gn_arg(*compiler_sources: str) -> bool:
    """Return whether this exact Chromium build exposes the /WX policy arg."""
    return any(
        bool(
            re.search(
                r"(?m)^\s*treat_warnings_as_errors\s*=",
                source,
            )
        )
        for source in compiler_sources
    )


def classify_version(
    v8_cache: RawSourceCache,
    build_cache: BuildSourceCache,
    clang_cache: BuildSourceCache,
    version: str,
) -> dict:
    try:
        deps = v8_cache.get(version, "DEPS")
        if deps is None:
            raise RuntimeError("V8 DEPS file was not found")
        toolset_spec = windows_legacy_toolset_spec(version)
        required_toolset = toolset_spec[1] if toolset_spec else "current"
        selected_vs_year = windows_compatibility_year(version)
        clang_revision = extract_clang_revision(deps)
        clang_update = clang_cache.get(clang_revision, CLANG_PATH)
        clang_release_version = extract_clang_release_version(clang_update)
        clang_dia_dll_years = extract_dia_dll_years(clang_update)
        clang_keyed_dia_dll = clang_hook_uses_keyed_dia_dll(clang_update)
        clang_vs_year_compatible = (
            not clang_keyed_dia_dll or selected_vs_year in clang_dia_dll_years
        )
        clang_toolset_compatible = clang_supports_selected_toolset(
            required_toolset, clang_release_version
        )
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
            compatible = (
                all(checks.values())
                and clang_vs_year_compatible
                and clang_toolset_compatible
            )
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
                "selected_vs_year": selected_vs_year,
                "vs_toolchain_years": ["2013", "2015"],
                "vs_year_compatible": selected_vs_year == "2015",
                "clang_revision": clang_revision,
                "clang_release_version": clang_release_version,
                "clang_dia_dll_years": clang_dia_dll_years,
                "clang_keyed_dia_dll": clang_keyed_dia_dll,
                "clang_vs_year_compatible": clang_vs_year_compatible,
                "clang_toolset_compatible": clang_toolset_compatible,
                "legacy_vcvars_reference_present": True,
                "legacy_vcvars_entry_point_provided": True,
                "toolset_injection_supported": checks["vs2015_compatibility"],
                "installed_sdk_injection_supported": checks["sdk_environment"],
                "linux_host_mode": linux_host_mode,
                "v8_deps_has_sysroot_hook": deps_has_sysroot_hook,
                "object_print_build_arg": "gyp:v8_object_print",
                "in_tree_gyp_checks": checks,
            }
        revision = extract_build_revision(deps)
        setup = build_cache.get(revision, BUILD_PATH)
        vs_toolchain = build_cache.get(revision, VS_TOOLCHAIN_PATH)
        compiler_build = build_cache.get(revision, COMPILER_BUILD_PATH)
        compiler_gni = ""
        if not supports_warning_policy_gn_arg(compiler_build):
            compiler_gni = build_cache.get(revision, COMPILER_GNI_PATH)
        warning_policy_location = (
            COMPILER_BUILD_PATH
            if supports_warning_policy_gn_arg(compiler_build)
            else COMPILER_GNI_PATH
            if supports_warning_policy_gn_arg(compiler_gni)
            else ""
        )
        warning_policy_arg_present = bool(warning_policy_location)
        vs_toolchain_years = extract_vs_toolchain_years(vs_toolchain)
        vs_year_compatible = selected_vs_year in vs_toolchain_years
        vs_bridge_active = bool(toolset_spec)
        vs_bridge_audit = (
            audit_vs_toolchain_patch(vs_toolchain, selected_vs_year)
            if vs_bridge_active
            else {
                "supported": True,
                "bridge_required": False,
                "checks": {},
                "error": "",
            }
        )
        v52_hosted_linker_checks = {}
        if version.startswith("5.2."):
            gcc_toolchain = build_cache.get(revision, GCC_TOOLCHAIN_PATH)
            v52_hosted_linker_checks = {
                "lld_flag_supported": '"-fuse-ld=lld"' in compiler_build,
                "lld_disables_gold_default": "!use_lld && is_linux" in compiler_build,
                "use_gold_forwarded": '"use_gold",' in gcc_toolchain,
                "use_lld_not_forwarded": '"use_lld",' not in gcc_toolchain,
            }
        build_gn = v8_cache.get(version, "BUILD.gn") or ""
        v8_gni = v8_cache.get(version, "gni/v8.gni") or ""
        object_print_arg = classify_object_print_gn_arg(build_gn, v8_gni)
        disassembler_arg_present = "v8_enable_disassembler" in (
            build_gn + "\n" + v8_gni
        )
        matches = list(WINDOWS_TOOLCHAIN_ARGS_RE.finditer(setup))
        environment_matches = list(WINDOWS_TOOLCHAIN_ENV_RE.finditer(setup))
        template = normalize_template(matches[0].group(0)) if len(matches) == 1 else ""
        legacy_vcvars_reference = bool(LEGACY_VCVARS_PATH_RE.search(setup))
        windows_compatible = len(matches) == 1 and len(environment_matches) == 1
        setup_patch_audit = audit_setup_toolchain_patch(setup)
        compatible = (
            windows_compatible
            and setup_patch_audit["supported"]
            and vs_year_compatible
            and vs_bridge_audit["supported"]
            and clang_vs_year_compatible
            and clang_toolset_compatible
            and all(v52_hosted_linker_checks.values())
            and warning_policy_arg_present
            and bool(object_print_arg)
            and disassembler_arg_present
        )
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
            "selected_vs_year": selected_vs_year,
            "vs_toolchain_years": vs_toolchain_years,
            "vs_year_compatible": vs_year_compatible,
            "vs_version_bridge_active": vs_bridge_active,
            "vs_version_bridge_required": bool(
                vs_bridge_active and vs_bridge_audit["bridge_required"]
            ),
            "vs_version_bridge_supported": vs_bridge_audit["supported"],
            "vs_version_bridge_checks": vs_bridge_audit["checks"],
            "vs_version_bridge_error": vs_bridge_audit["error"],
            "clang_revision": clang_revision,
            "clang_release_version": clang_release_version,
            "clang_dia_dll_years": clang_dia_dll_years,
            "clang_keyed_dia_dll": clang_keyed_dia_dll,
            "clang_vs_year_compatible": clang_vs_year_compatible,
            "clang_toolset_compatible": clang_toolset_compatible,
            **(
                {"v52_hosted_linker_checks": v52_hosted_linker_checks}
                if v52_hosted_linker_checks
                else {}
            ),
            "legacy_vcvars_reference_present": legacy_vcvars_reference,
            "legacy_vcvars_entry_point_provided": bool(
                toolset_spec and legacy_vcvars_reference
            ),
            "toolset_injection_supported": setup_patch_audit["supported"],
            "installed_sdk_injection_supported": len(environment_matches) == 1,
            "setup_toolchain_patch_checks": setup_patch_audit["checks"],
            "setup_toolchain_um_lib_fallback_anchor_present": setup_patch_audit[
                "um_lib_fallback_anchor_present"
            ],
            "setup_toolchain_um_lib_fallback_active": bool(
                toolset_spec and setup_patch_audit["um_lib_fallback_anchor_present"]
            ),
            "setup_toolchain_patch_error": setup_patch_audit["error"],
            "warnings_as_errors_build_arg_present": warning_policy_arg_present,
            "warnings_as_errors_build_arg_location": warning_policy_location,
            "linux_host_mode": linux_host_mode,
            "v8_deps_has_sysroot_hook": deps_has_sysroot_hook,
            "object_print_build_arg": object_print_arg,
            "disassembler_build_arg_present": disassembler_arg_present,
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
        f"Chromium clang-hook revisions: **{summary['clang_revisions']}**",
        "",
        f"Windows toolchain templates: **{summary['templates']}**",
        "",
        "External-GN tags whose legacy setup transform was replayed, "
        "tokenized, and found idempotent: "
        f"**{summary['setup_patch_replay_tags']}**",
        "",
        "External-GN tags preserving SDK-before-toolset argument order: "
        f"**{summary['setup_patch_sdk_ordering_tags']}**",
        "",
        "External-GN tags with an exact SDK UM-library fallback anchor: "
        f"**{summary['setup_patch_um_lib_fallback_anchor_tags']}**",
        "",
        "Historical-toolset tags that actively receive that fallback: "
        f"**{summary['setup_patch_um_lib_fallback_active_tags']}**",
        "",
        "Historical-toolset tags replaying the VS-version bridge: "
        f"**{summary['vs_version_bridge_replay_tags']}**",
        "",
        "Exact tags that require the VS-version bridge on a VS 2022 host: "
        f"**{summary['vs_version_bridge_required_tags']}**",
        "",
        "Bridge-required tags whose pinned clang hook consumes the logical "
        "year for keyed DIA lookup: "
        f"**{summary['vs_version_bridge_keyed_dia_tags']}**",
        "",
        "Pinned clang releases recorded: "
        + ", ".join(
            f"`{release}` **{count}**"
            for release, count in summary["clang_release_counts"].items()
        ),
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
        "Object-print build arguments: "
        + ", ".join(
            f"`{name}` **{count}**"
            for name, count in summary["object_print_build_args"].items()
        ),
        "",
        "External-GN tags with exact `treat_warnings_as_errors` support: "
        f"**{summary['warning_policy_gn_tags']}**",
        "",
        "Warning-policy declaration locations: "
        + ", ".join(
            f"`{path}` **{count}**"
            for path, count in summary["warning_policy_locations"].items()
        ),
        "",
        "Selected Visual Studio compatibility years: "
        + ", ".join(
            f"`{year}` **{count}**"
            for year, count in summary["vs_year_counts"].items()
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
        "routes the pinned clang and gold paths to the hosted compiler and lld. "
        "Every later exact "
        "tag must match one `vcvarsall` argument template and one environment-"
        "capture call, so CI can select both the historical MSVC headers and "
        "the SDK version actually installed on the runner. "
        "The build selects v142 for V8 5.x, v141 for 6.x–7.x and "
            "8.0–8.1, v142 for 8.2–9.x, and the current toolset for "
            "10.x–11.x. This boundary is checked against the exact pinned "
            "clang release; in particular, clang 10 is not paired with the "
            "v142 headers that require clang 11. For every tag "
            "where a historical toolset is selected and the pinned setup "
            "script retains the legacy path, CI provides a forwarding "
            "`VC/vcvarsall.bat` entry point. The exact Chromium build and "
            "tools/clang revisions are also checked to ensure the selected "
            "Visual Studio year is accepted by both `vs_toolchain.py` and "
            "the clang hook's keyed DIA DLL table.",
            "For every external-GN tag, the setup-toolchain transform used by "
            "the legacy production path is applied to the exact Chromium "
            "source, applied a second time "
            "to prove idempotence, and fully tokenized. This catches dangling "
            "continuation lines in multi-line ATL/MFC assertions before an "
            "Actions build starts. The replay also requires the installed SDK "
            "argument to remain ahead of the `-vcvars_ver` toolset switch, "
            "matching the upstream vcvars template order. The replay preserves "
            "the exact original UM-library and linker output statements, which "
            "guards against an assertion edit consuming unrelated code. Active "
            "historical-toolset templates that export `vc_lib_um_path` also "
            "receive a checked fallback to the installed SDK's "
            "`Lib/<version>/um/<arch>` directory if vcvars omits it. The exact "
            "`vs_toolchain.py` helper is separately replayed so revisions that "
            "stopped honoring `GYP_MSVS_VERSION` still expose the selected "
            "logical VS2017/2019 generation on the VS 2022 runner. Where the "
            "exact pinned clang hook performs a keyed DIA lookup, the same "
            "logical year is verified against that hook's table.",
            "Every external-GN tag is also checked against its exact Chromium "
            "compiler configuration before CI disables warnings-as-errors on "
            "Windows. This keeps modern hosted MSVC diagnostics from becoming "
            "build failures without editing V8 source or hiding compiler errors.",
            "The JSON report records the exact V8 tag, Chromium build revision, "
            "template, and compatibility result.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, default=Path("compat/legacy-v8-api.json"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/audit/legacy-v8-host-tools.json"),
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path("artifacts/audit/legacy-v8-host-tools.md"),
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
    parser.add_argument(
        "--clang-cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "jsc2js-v8-clang-hook-audit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_audit = json.loads(args.audit.read_text(encoding="utf-8"))
    versions = [record["version"] for record in api_audit["versions"]]
    v8_cache = RawSourceCache(args.v8_cache_dir)
    build_cache = BuildSourceCache(args.build_cache_dir)
    clang_cache = BuildSourceCache(args.clang_cache_dir, root=CLANG_ROOT)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(
            executor.map(
                lambda version: classify_version(
                    v8_cache, build_cache, clang_cache, version
                ),
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
            "clang_revisions": len(
                {
                    result.get("clang_revision")
                    for result in results
                    if result.get("clang_revision")
                }
            ),
            "clang_release_counts": {
                release: sum(
                    result.get("clang_release_version") == release
                    for result in results
                )
                for release in sorted(
                    {
                        result.get("clang_release_version")
                        for result in results
                        if result.get("clang_release_version")
                    },
                    key=lambda release: (
                        int(release.split(".", 1)[0]),
                        release,
                    ),
                )
            },
            "clang_toolset_compatible_tags": sum(
                bool(result.get("clang_toolset_compatible")) for result in results
            ),
            "templates": len(families),
            "setup_patch_replay_tags": sum(
                bool(result.get("setup_toolchain_patch_checks"))
                and bool(result.get("toolset_injection_supported"))
                for result in results
            ),
            "setup_patch_tokenizable_tags": sum(
                bool(
                    result.get("setup_toolchain_patch_checks", {}).get("tokenizable")
                )
                for result in results
            ),
            "setup_patch_idempotent_tags": sum(
                bool(
                    result.get("setup_toolchain_patch_checks", {}).get("idempotent")
                )
                for result in results
            ),
            "setup_patch_sdk_ordering_tags": sum(
                bool(
                    result.get("setup_toolchain_patch_checks", {}).get(
                        "sdk_precedes_vcvars_guard"
                    )
                )
                for result in results
            ),
            "setup_patch_um_lib_fallback_anchor_tags": sum(
                bool(result.get("setup_toolchain_um_lib_fallback_anchor_present"))
                for result in results
            ),
            "setup_patch_um_lib_fallback_active_tags": sum(
                bool(result.get("setup_toolchain_um_lib_fallback_active"))
                for result in results
            ),
            "vs_version_bridge_replay_tags": sum(
                bool(result.get("vs_version_bridge_active"))
                and bool(result.get("vs_version_bridge_supported"))
                for result in results
            ),
            "vs_version_bridge_required_tags": sum(
                bool(result.get("vs_version_bridge_required"))
                for result in results
            ),
            "vs_version_bridge_keyed_dia_tags": sum(
                bool(result.get("vs_version_bridge_required"))
                and bool(result.get("clang_keyed_dia_dll"))
                for result in results
            ),
            "toolset_counts": {
                toolset: sum(
                    result.get("required_toolset") == toolset for result in results
                )
                for toolset in ("v140", "v141", "v142", "current")
            },
            "vs_year_counts": {
                year: sum(result.get("selected_vs_year") == year for result in results)
                for year in ("2015", "2017", "2019", "2022")
            },
            "keyed_clang_dia_dll_tags": sum(
                bool(result.get("clang_keyed_dia_dll")) for result in results
            ),
            "legacy_vcvars_entry_point_tags": sum(
                bool(result.get("legacy_vcvars_entry_point_provided"))
                for result in results
            ),
            "warning_policy_gn_tags": sum(
                bool(result.get("warnings_as_errors_build_arg_present"))
                for result in results
            ),
            "warning_policy_locations": {
                path: sum(
                    result.get("warnings_as_errors_build_arg_location") == path
                    for result in results
                )
                for path in (COMPILER_BUILD_PATH, COMPILER_GNI_PATH)
            },
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
            "object_print_build_args": {
                name: sum(
                    result.get("object_print_build_arg") == name
                    for result in results
                )
                for name in sorted(
                    {
                        result.get("object_print_build_arg")
                        for result in results
                        if result.get("object_print_build_arg")
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
