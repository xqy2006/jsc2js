#!/usr/bin/env python3
"""
Batch build script (v8gen-only + per-version backup of out.gn/x64.release).

For each version:
  - fetch tags, checkout tag
  - gclient sync -D --no-history && gclient runhooks
  - remove existing v8/out.gn/x64.release (unless KEEP_WORK_DIR=1)
  - write a cross-version x64.release args.gn and run gn gen directly
  - ninja -C out.gn/x64.release d8
  - copy d8 binary AND snapshot_blob.bin to artifacts/d8-<version>-<OS>/
  - backup directory:
       out.gn/x64.release  -->  out.gn/version_backups/x64.release.<sanitized_version>
    (sanitized_version = version with '.' replaced by '_')
  - optional compression if BACKUP_COMPRESS=1:
       Linux: tar + zstd => x64.release.<sanitized_version>.tar.zst
       Windows: zip archive
    then delete the uncompressed backup directory.

Env vars:
  ASSIGNED_JSON         JSON array of versions
  APPLY_SCRIPT_NAME     (default apply_patch.py)
  BACKUP_BASE           (default: out.gn/version_backups)
  BACKUP_COMPRESS       "1" to compress backups
  KEEP_WORK_DIR         "1" to reuse existing x64.release directory (won't delete before rebuild)
  SKIP_BACKUP           "1" to skip copying the full build directory (validation workflows)
"""
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import traceback

EXPECTED_FILES = {
    "src/d8/d8.cc",
    "src/d8/d8.h",
    "src/diagnostics/objects-printer.cc",
    "src/objects/string.cc",
    "src/snapshot/code-serializer.cc",
    "src/snapshot/deserializer.cc",
}

WINDOWS_TOOLCHAIN_ARGS_RE = re.compile(
    r"^(?P<indent>[ \t]+)args = \[script_path,"
    r"(?:[^\]\r\n]*\r?\n[ \t]+)*[^\]\r\n]*\][ \t]*$",
    re.MULTILINE,
)
WINDOWS_TOOLCHAIN_ENV_RE = re.compile(
    r"^(?P<indent>[ \t]+)variables = _LoadEnvFromBat\(args\)[ \t]*$",
    re.MULTILINE,
)
WINDOWS_ATLMFC_ASSERT_RE = re.compile(
    r"^(?P<indent>[ \t]+)assert vc_lib_atlmfc_path"
    r"(?:,[^\r\n]*\r?\n[ \t]+[^\r\n]*)?[ \t]*$",
    re.MULTILINE,
)
WINDOWS_UM_LIB_OUTPUT_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?=(?:assert vc_lib_um_path|"
    r"print(?:[ \t]*\(|[ \t]+)['\"]vc_lib_um_path))",
    re.MULTILINE,
)
WINDOWS_VS_VERSION_FUNCTION_RE = re.compile(
    r"(?ms)^def GetVisualStudioVersion\(\):.*?(?=^def |\Z)"
)
WINDOWS_VS_VERSION_HEADER_RE = re.compile(
    r"^(?P<header>def GetVisualStudioVersion\(\):[^\r\n]*\r?\n)"
    r"(?P<body_indent>[ \t]+)(?=\S)",
    re.MULTILINE,
)
WINDOWS_VCVARS_MARKER = "# JSC2JS_LEGACY_VCVARS_VERSION"
WINDOWS_SDK_MARKER = "# JSC2JS_INSTALLED_WINDOWS_SDK"
WINDOWS_ATLMFC_MARKER = "# JSC2JS_OPTIONAL_ATLMFC"
WINDOWS_UM_LIB_MARKER = "# JSC2JS_INSTALLED_SDK_UM_LIB"
WINDOWS_VS_VERSION_MARKER = "# JSC2JS_HOSTED_VS_VERSION"

def log(msg: str):
    print(f"[{datetime.utcnow().isoformat()}] {msg}")

def run(cmd: str, cwd: str = None, check: bool = True) -> int:
    log(f"RUN: {cmd}")
    r = subprocess.run(cmd, cwd=cwd, shell=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {cmd}")
    return r.returncode

def git_diff_files() -> set:
    out = subprocess.check_output(
        "git -C v8 diff --name-only", shell=True, text=True, stderr=subprocess.STDOUT
    )
    return {line.strip() for line in out.splitlines() if line.strip()}

def write_list(path: str, items):
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(it + "\n")


def select_patch_implementation(version: str) -> tuple[str, str]:
    """Return (strategy, path) for one exact V8 tag."""
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", version):
        raise ValueError(f"invalid V8 tag: {version!r}")
    parts = tuple(int(part) for part in version.split("."))
    number = parts + (0,) * (4 - len(parts))
    major, minor, patch, _ = number
    if major < 12:
        return "legacy-semantic", "patches/legacy/apply_legacy_patch.py"
    if number >= (14, 7, 84, 0):
        return "modern-semantic", "patches/modern/apply_modern_patch.py"
    if major == 12 and minor < 6:
        return "unified-diff", "patches/current/v8-12.0-to-12.5.patch"
    if major < 13 or (major == 13 and (minor < 2 or (minor == 2 and patch < 135))):
        return "unified-diff", "patches/current/v8-12.6-to-13.2.134.patch"
    return "unified-diff", "patches/current/v8-13.2.135-to-14.7.83.patch"


def restore_version_worktrees(v8_root: Path = Path("v8")):
    """Restore tracked compatibility edits in V8 and its //build checkout."""
    roots = [v8_root]
    external_build = v8_root / "build"
    if (external_build / ".git").exists():
        roots.append(external_build)
    for root in roots:
        log(f"Restoring tracked files under {root}")
        completed = subprocess.run(["git", "-C", str(root), "checkout", "."])
        if completed.returncode != 0:
            log(f"WARNING: could not restore tracked files under {root}")


def collect_audit_records(
    artifacts_dir: Path,
    version: str,
    os_name: str,
    error: str = "",
    v8_root: Path = Path("v8"),
) -> Path:
    """Preserve patch reports and a traceback even when a batch tag fails."""
    target_dir = artifacts_dir / f"d8-{version}-{os_name}"
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in ("apply_patch_report.txt", "apply_patch_report.json"):
        report = v8_root / name
        if report.is_file():
            shutil.copy2(report, target_dir / name)
    if error:
        (target_dir / "build_error.txt").write_text(
            error.rstrip() + "\n", encoding="utf-8", newline="\n"
        )
    return target_dir


def copytree(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

def compress_backup(path: Path):
    system = platform.system().lower()
    if system.startswith("linux"):
        # tar + zstd
        tar_name = path.with_suffix(".tar.zst")
        cmd = f"tar --use-compress-program=zstd -cf {tar_name.name} {path.name}"
        run(cmd, cwd=str(path.parent), check=True)
        shutil.rmtree(path, ignore_errors=True)
        return tar_name
    else:
        # Windows / others: zip
        zip_name = path.with_suffix(".zip")
        shutil.make_archive(str(path), 'zip', root_dir=str(path))
        shutil.rmtree(path, ignore_errors=True)
        return zip_name


def rejection_cache_fixtures() -> dict[str, bytes]:
    """Return malformed modern-cache shapes that must be rejected up front."""
    bad_magic = bytearray(48)
    struct.pack_into("<I", bad_magic, 0, 0x12345678)
    struct.pack_into("<I", bad_magic, 20, 16)

    bad_length = bytearray(48)
    struct.pack_into("<I", bad_length, 0, 0xC0DE0000)
    struct.pack_into("<I", bad_length, 20, 0)
    return {
        "short": b"not-v8",
        "magic-family": bytes(bad_magic),
        "payload-length": bytes(bad_length),
    }


def run_rejection_smoke(built_bin: Path) -> str:
    """Verify malformed cache data is rejected without aborting the process."""
    build_dir = built_bin.parent.resolve()
    marker = "JSC2JS_SAFE_REJECTION"
    javascript_parts = []
    expected_markers = []
    for label, content in rejection_cache_fixtures().items():
        filename = f"jsc2js-invalid-{label}.jsc"
        (build_dir / filename).write_bytes(content)
        expected = f"{marker}:{label}:"
        expected_markers.append(expected)
        javascript_parts.append(
            f"try {{ loadjsc('{filename}'); "
            f"print('JSC2JS_UNEXPECTED_ACCEPT:{label}'); quit(3); }} "
            f"catch (error) {{ print('{expected}' + error); }}"
        )
    completed = subprocess.run(
        [str(built_bin.resolve()), "-e", "".join(javascript_parts)],
        cwd=str(build_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    output = completed.stdout or ""
    if (
        completed.returncode != 0
        or any(expected not in output for expected in expected_markers)
        or "JSC2JS_UNEXPECTED_ACCEPT" in output
    ):
        raise RuntimeError(
            "malformed-cache smoke test failed: "
            f"exit={completed.returncode} output={output[-2000:]}"
        )
    return output


def run_valid_cache_smoke(built_bin: Path, cache_path: Path) -> str:
    """Verify a real cache from the matching Electron/V8 build is printable."""
    if not cache_path.is_file() or cache_path.stat().st_size == 0:
        raise RuntimeError(f"legacy valid-cache fixture is missing: {cache_path}")
    build_dir = built_bin.parent.resolve()
    local_cache = build_dir / "jsc2js-valid-cache.jsc"
    shutil.copy2(cache_path, local_cache)
    marker = "JSC2JS_VALID_CACHE_OK"
    javascript = (
        "loadjsc('jsc2js-valid-cache.jsc'); "
        f"print('{marker}');"
    )
    completed = subprocess.run(
        [
            str(built_bin.resolve()),
            "--no-lazy",
            "--profile-deserialization",
            "-e",
            javascript,
        ],
        cwd=str(build_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    output = completed.stdout or ""
    if (
        completed.returncode != 0
        or marker not in output
        or "Start SharedFunctionInfo" not in output
    ):
        raise RuntimeError(
            "valid-cache smoke test failed: "
            f"exit={completed.returncode} output={output[-4000:]}"
        )
    return output


def valid_cache_for_version(version: str) -> Path | None:
    """Resolve an optional per-version cross-embedder cache fixture."""
    mapping_file = os.environ.get("JSC2JS_VALID_CACHE_MAP_FILE", "").strip()
    if mapping_file:
        path = Path(mapping_file)
        try:
            mapping = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid valid-cache mapping {path}: {error}")
        fixture = mapping.get(version) if isinstance(mapping, dict) else None
        if fixture:
            return Path(fixture).resolve()
    fallback = os.environ.get("JSC2JS_VALID_CACHE", "").strip()
    fallback_version = os.environ.get("JSC2JS_VALID_CACHE_VERSION", "").strip()
    if fallback and (not fallback_version or fallback_version == version):
        return Path(fallback).resolve()
    return None


def configure_host_compatibility():
    """Make historical LLVM binaries usable on current Linux runners."""
    if not platform.system().lower().startswith("linux"):
        return
    try:
        system_libstdcpp = subprocess.check_output(
            ["g++", "-print-file-name=libstdc++.so.6"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        system_libstdcpp = ""
    if system_libstdcpp and system_libstdcpp != "libstdc++.so.6":
        library_dir = str(Path(system_libstdcpp).resolve().parent)
        current = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = (
            library_dir if not current else library_dir + os.pathsep + current
        )
        log(f"Preferring host libstdc++ for historical LLVM: {library_dir}")

def configure_python_compatibility():
    compat_dir = Path("tools/python_compat").resolve()
    current = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        str(compat_dir)
        if not current
        else str(compat_dir) + os.pathsep + current
    )


def activate_legacy_hook_python(version: str):
    """Put Python 2 ahead of depot_tools only for V8 hooks that require it."""
    python2_dir = os.environ.get("JSC2JS_PYTHON2_DIR", "")
    if int(version.split(".", 1)[0]) >= 9:
        # A batch can cross the V8 8.x -> 9.x boundary. Do not leak the
        # previous version's Python 2 interpreter into modern hooks.
        if python2_dir:
            normalized = os.path.normcase(os.path.normpath(python2_dir))
            os.environ["PATH"] = os.pathsep.join(
                entry
                for entry in os.environ.get("PATH", "").split(os.pathsep)
                if os.path.normcase(os.path.normpath(entry)) != normalized
            )
        os.environ.pop("JSC2JS_HOOK_PYTHON", None)
        return
    if not python2_dir or not Path(python2_dir).is_dir():
        raise RuntimeError(
            f"V8 {version} requires JSC2JS_PYTHON2_DIR for its historical hooks"
        )
    os.environ["PATH"] = python2_dir + os.pathsep + os.environ.get("PATH", "")
    resolved = shutil.which("python")
    if not resolved:
        raise RuntimeError("Python 2 shim did not provide a python command")
    version_output = subprocess.check_output(
        [resolved, "--version"], stderr=subprocess.STDOUT, text=True
    ).strip()
    if not version_output.startswith("Python 2.7"):
        raise RuntimeError(
            f"Historical V8 hooks resolved {resolved} as {version_output}, not Python 2.7"
        )
    os.environ["JSC2JS_HOOK_PYTHON"] = resolved
    os.environ["DEPOT_TOOLS_UPDATE"] = "0"
    log(f"Using {resolved} ({version_output}) for V8 {version} hooks")
    return resolved


def patch_gclient_hook_dispatch(hook_python: str):
    """Make current depot_tools honor the interpreter required by old DEPS."""
    gclient_command = shutil.which("gclient")
    if not gclient_command:
        raise RuntimeError("gclient command was not found")
    gclient_py = Path(gclient_command).resolve().with_name("gclient.py")
    if not gclient_py.is_file():
        raise RuntimeError(f"Could not locate gclient.py beside {gclient_command}")

    marker = "# JSC2JS_LEGACY_HOOK_PYTHON"
    source = gclient_py.read_text(encoding="utf-8")
    if marker not in source:
        anchor_pattern = re.compile(
            r"^(?P<indent>[ \t]+)cmd = "
            r"(?:list\(self\._action\)|\[arg for arg in self\._action\])\s*$",
            re.MULTILINE,
        )
        match = anchor_pattern.search(source)
        if not match:
            raise RuntimeError(
                f"Unsupported depot_tools Hook.run layout in {gclient_py}"
            )
        indent = match.group("indent")
        injection = (
            match.group(0)
            + f"\n{indent}{marker}\n"
            + f'{indent}hook_python = os.environ.get("JSC2JS_HOOK_PYTHON")\n'
            + f'{indent}if hook_python and cmd and cmd[0] == "python":\n'
            + f"{indent}    cmd[0] = hook_python\n"
            + f"{indent}    hook_dir = os.path.dirname(hook_python)\n"
            + f'{indent}    current_path = os.environ.get("PATH", "")\n'
            + f"{indent}    if current_path.split(os.pathsep)[0] != hook_dir:\n"
            + f'{indent}        os.environ["PATH"] = hook_dir + os.pathsep + current_path'
        )
        source = source[: match.start()] + injection + source[match.end() :]
        gclient_py.write_text(source, encoding="utf-8")
    os.environ["JSC2JS_HOOK_PYTHON"] = hook_python
    log(f"Configured {gclient_py} to dispatch legacy hooks with {hook_python}")


def windows_legacy_toolset_spec(version: str):
    """Return the installed MSVC header generation matching a V8 release era."""
    major, minor = (int(part) for part in version.split(".", 2)[:2])
    if major == 5:
        return "14.29.*", "v142"
    if major < 8 or (major == 8 and minor < 2):
        return "14.16.*", "v141"
    if major < 10:
        return "14.29.*", "v142"
    return None


def windows_compatibility_year(version: str) -> str:
    """Return the Visual Studio generation understood by this V8 branch."""
    major = int(version.split(".", 1)[0])
    if major == 5:
        return "2015"
    if major < 8:
        return "2017"
    if major < 10:
        return "2019"
    return "2022"


def uses_in_tree_gyp(version: str) -> bool:
    """V8 5.1 predates the standalone Chromium //build GN dependency."""
    major, minor = (int(part) for part in version.split(".", 2)[:2])
    return (major, minor) == (5, 1)


def configure_v8_52_linux_gn(
    version: str, v8_root: Path = Path("v8")
) -> str:
    """Use hosted tools for V8 5.2, whose DEPS predates the sysroot hook."""
    if not platform.system().lower().startswith("linux"):
        return ""
    major, minor = (int(part) for part in version.split(".", 2)[:2])
    if (major, minor) != (5, 2):
        return ""

    clang = shutil.which("clang")
    clangxx = shutil.which("clang++")
    if not clang or not clangxx:
        raise RuntimeError("V8 5.2 requires hosted clang and clang++")
    bundled_bin = (
        v8_root / "third_party/llvm-build/Release+Asserts/bin"
    )
    if not bundled_bin.is_dir():
        raise RuntimeError(f"V8 5.2 bundled clang directory is missing: {bundled_bin}")
    for name, executable in (("clang", clang), ("clang++", clangxx)):
        wrapper = bundled_bin / name
        # The historical clang archive may store clang and clang++ as hard
        # links. Unlink first so writing one wrapper cannot replace both.
        wrapper.unlink(missing_ok=True)
        wrapper.write_text(
            f"#!/bin/sh\nexec {shlex.quote(executable)} \"$@\"\n",
            encoding="utf-8",
            newline="\n",
        )
        wrapper.chmod(0o755)
    log(f"Using hosted clang for V8 {version}: {clang}, {clangxx}")
    # Do not set use_gold here. This build revision forwards an invoker value
    # with that name inside clang_toolchain(), and old GN rejects a global arg
    # that would clobber it before generation starts.
    return (
        "use_sysroot = false\n"
        "clang_use_chrome_plugins = false\n"
        "treat_warnings_as_errors = false\n"
        # V8 5.2's bundled gold predates relocation 42 emitted by the hosted
        # clang. Its exact compiler config supports use_lld without forwarding
        # that name through the conflicting clang_toolchain() invoker scope.
        "use_lld = true\n"
        "linux_use_bundled_binutils = false\n"
    )


def object_print_gn_arg(v8_root: Path = Path("v8")) -> str:
    """Select the object-print build-arg spelling declared by this V8 tag."""
    build_gn = v8_root / "BUILD.gn"
    if not build_gn.is_file():
        raise RuntimeError(f"V8 BUILD.gn is missing: {build_gn}")
    source = build_gn.read_text(encoding="utf-8", errors="replace")
    if re.search(r"(?m)^\s*v8_object_print\s*=", source):
        return "v8_object_print = true\n"
    return "v8_enable_object_print = true\n"


def windows_warning_policy_gn_arg(v8_root: Path = Path("v8")) -> str:
    """Disable upstream /WX when a modern hosted MSVC builds legacy V8."""
    if not platform.system().lower().startswith("win"):
        return ""
    compiler_configs = (
        v8_root / "build/config/compiler/BUILD.gn",
        v8_root / "build/config/compiler/compiler.gni",
    )
    sources = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in compiler_configs
        if path.is_file()
    ]
    if not any(
        re.search(r"(?m)^\s*treat_warnings_as_errors\s*=", source)
        for source in sources
    ):
        raise RuntimeError(
            "The exact Chromium build revision does not declare "
            "treat_warnings_as_errors"
        )
    return "treat_warnings_as_errors = false\n"


def configure_in_tree_gyp(version: str) -> bool:
    """Select V8 5.1's native GYP/Ninja generator, or clear stale batch state."""
    if not uses_in_tree_gyp(version):
        for name in ("GYP_GENERATORS", "GYP_GENERATOR_FLAGS", "GYP_DEFINES"):
            os.environ.pop(name, None)
        return False

    os.environ["GYP_GENERATORS"] = "ninja"
    os.environ["GYP_GENERATOR_FLAGS"] = "output_dir=out"
    defines = [
        "target_arch=x64",
        "v8_target_arch=x64",
        "v8_enable_disassembler=1",
        "v8_object_print=1",
        "v8_use_external_startup_data=1",
        "component=static_library",
    ]
    if platform.system().lower().startswith("linux"):
        # The downloaded 2016 clang cannot parse the GCC 11 standard library
        # on ubuntu-22.04. V8 5.1's standalone.gypi explicitly supports a
        # custom clang_dir, so use the runner's compatible /usr/bin/clang.
        defines.extend(
            (
                "clang=1",
                "host_clang=1",
                "clang_dir=/usr",
                "linux_use_bundled_gold=0",
                "werror=",
            )
        )
    os.environ["GYP_DEFINES"] = " ".join(defines)
    log(f"Using the in-tree GYP/Ninja generator for V8 {version}")
    return True


def provide_legacy_vcvars_entry_point(vs_root: Path) -> Path:
    """Bridge pre-VS-2017 vcvars paths to a current hosted VS installation."""
    vcvars = vs_root / "VC/Auxiliary/Build/vcvarsall.bat"
    if not vcvars.is_file():
        raise RuntimeError(f"vcvarsall.bat was not found at {vcvars}")
    legacy_vcvars = vs_root / "VC/vcvarsall.bat"
    if not legacy_vcvars.is_file():
        legacy_vcvars.write_text(
            '@call "%~dp0Auxiliary\\Build\\vcvarsall.bat" %*\n',
            encoding="ascii",
            newline="\r\n",
        )
        log(f"Provided the legacy GYP/GN vcvars entry point at {legacy_vcvars}")
    return legacy_vcvars


def activate_windows_vcvars(version: str):
    """Import the selected hosted MSVC/SDK environment for the V8 5.1 GYP build."""
    if not platform.system().lower().startswith("win"):
        return
    spec = windows_legacy_toolset_spec(version)
    if spec is None:
        return
    toolset_glob, toolset_name = spec
    vs_root = Path(os.environ.get("GYP_MSVS_OVERRIDE_PATH", ""))
    compatible = sorted(
        path for path in (vs_root / "VC/Tools/MSVC").glob(toolset_glob) if path.is_dir()
    )
    if not compatible:
        raise RuntimeError(f"MSVC {toolset_name} was not found under {vs_root}")
    vcvars_version = ".".join(compatible[-1].name.split(".")[:2])
    vcvars = vs_root / "VC/Auxiliary/Build/vcvarsall.bat"
    provide_legacy_vcvars_entry_point(vs_root)
    activation_signature = f"{vs_root.resolve()}|{vcvars_version}"
    if os.environ.get("JSC2JS_ACTIVE_VCVARS_SIGNATURE") == activation_signature:
        log(
            f"Reusing imported MSVC {toolset_name} ({compatible[-1].name}) "
            f"for V8 {version} GYP"
        )
        return
    sdk_version = os.environ.get("JSC2JS_WINDOWS_SDK_VERSION", "")
    # Let vcvarsall select its installed default SDK. Passing a current SDK
    # through the historical -winsdk switch makes VS 2022's v142 setup fail
    # before GYP starts, even though the SDK itself is usable by the build.
    command = f'call "{vcvars}" x64 -vcvars_ver={vcvars_version} >nul && set'
    completed = subprocess.run(
        f'cmd.exe /d /s /c "{command}"',
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"vcvarsall failed with exit {completed.returncode}: "
            f"stdout={completed.stdout[-2000:]!r} "
            f"stderr={completed.stderr[-2000:]!r}"
        )
    for line in completed.stdout.splitlines():
        if "=" not in line or line.startswith("="):
            continue
        name, value = line.split("=", 1)
        os.environ[name] = value
    os.environ["GYP_MSVS_VERSION"] = "2015"
    os.environ["GYP_MSVS_OVERRIDE_PATH"] = str(vs_root)
    os.environ["JSC2JS_ACTIVE_VCVARS_SIGNATURE"] = activation_signature
    log(
        f"Imported MSVC {toolset_name} ({compatible[-1].name}) and SDK "
        f"{sdk_version or 'default'} for V8 {version} GYP"
    )


def patch_windows_setup_toolchain_source(source: str) -> str:
    """Inject hosted MSVC/SDK selection into one exact Chromium template."""
    if WINDOWS_VCVARS_MARKER not in source:
        matches = list(WINDOWS_TOOLCHAIN_ARGS_RE.finditer(source))
        if len(matches) != 1:
            raise RuntimeError(
                "Unsupported setup_toolchain.py vcvars argument layout: "
                f"found {len(matches)} anchors"
            )
        match = matches[0]
        indent = match.group("indent")
        injection = (
            match.group(0)
            + f"\n{indent}{WINDOWS_VCVARS_MARKER}\n"
            + f"{indent}jsc2js_vcvars_version = "
            + "os.environ.get('JSC2JS_VCVARS_VERSION')\n"
            + f"{indent}if jsc2js_vcvars_version:\n"
            + f"{indent}  args.append('-vcvars_ver=' + jsc2js_vcvars_version)"
        )
        source = source[: match.start()] + injection + source[match.end() :]

    if WINDOWS_SDK_MARKER not in source:
        matches = list(WINDOWS_TOOLCHAIN_ENV_RE.finditer(source))
        if len(matches) != 1:
            raise RuntimeError(
                "Unsupported setup_toolchain.py SDK environment layout: "
                f"found {len(matches)} anchors"
            )
        match = matches[0]
        indent = match.group("indent")
        injection = (
            f"{indent}{WINDOWS_SDK_MARKER}\n"
            f"{indent}jsc2js_sdk_version = "
            "os.environ.get('JSC2JS_WINDOWS_SDK_VERSION')\n"
            f"{indent}if jsc2js_sdk_version:\n"
            f"{indent}  args = [arg for arg in args if not (\n"
            f"{indent}      isinstance(arg, str) and arg.count('.') == 3 and\n"
            f"{indent}      all(part.isdigit() for part in arg.split('.')))]\n"
            f"{indent}  jsc2js_vcvars_index = next((\n"
            f"{indent}      index for index, arg in enumerate(args)\n"
            f"{indent}      if isinstance(arg, str) and\n"
            f"{indent}      arg.startswith('-vcvars_ver=')), len(args))\n"
            f"{indent}  args.insert(jsc2js_vcvars_index, jsc2js_sdk_version)\n"
            + match.group(0)
        )
        source = source[: match.start()] + injection + source[match.end() :]

    if (
        WINDOWS_ATLMFC_MARKER not in source
        and "assert vc_lib_atlmfc_path" in source
    ):
        matches = list(WINDOWS_ATLMFC_ASSERT_RE.finditer(source))
        if len(matches) != 1:
            raise RuntimeError(
                "Unsupported setup_toolchain.py ATL/MFC assertion layout: "
                f"found {len(matches)} anchors"
            )
        match = matches[0]
        indent = match.group("indent")
        replacement = (
            f"{indent}pass  {WINDOWS_ATLMFC_MARKER}: "
            "d8 does not link the optional ATL/MFC libraries"
        )
        source = source[: match.start()] + replacement + source[match.end() :]

    if WINDOWS_UM_LIB_MARKER not in source and "vc_lib_um_path" in source:
        matches = list(WINDOWS_UM_LIB_OUTPUT_RE.finditer(source))
        if not matches:
            raise RuntimeError(
                "Unsupported setup_toolchain.py Windows SDK UM library layout"
            )
        match = matches[0]
        indent = match.group("indent")
        injection = (
            f"{indent}{WINDOWS_UM_LIB_MARKER}\n"
            f"{indent}jsc2js_sdk_version = "
            "os.environ.get('JSC2JS_WINDOWS_SDK_VERSION')\n"
            f"{indent}if not vc_lib_um_path and jsc2js_sdk_version:\n"
            f"{indent}  jsc2js_um_lib_path = os.path.join(\n"
            f"{indent}      win_sdk_path, 'Lib', jsc2js_sdk_version,\n"
            f"{indent}      'um', target_cpu)\n"
            f"{indent}  if os.path.isfile(os.path.join(\n"
            f"{indent}      jsc2js_um_lib_path, 'User32.Lib')):\n"
            f"{indent}    vc_lib_um_path = os.path.realpath(jsc2js_um_lib_path)\n"
        )
        source = source[: match.start()] + injection + source[match.start() :]

    return source


def patch_windows_vs_toolchain_source(source: str) -> str:
    """Make a selected historical VS year visible on a VS 2022 host."""
    function = WINDOWS_VS_VERSION_FUNCTION_RE.search(source)
    if not function:
        raise RuntimeError("Unsupported vs_toolchain.py Visual Studio version layout")
    if (
        WINDOWS_VS_VERSION_MARKER in source
        or "GYP_MSVS_VERSION" in function.group(0)
    ):
        return source
    if "MSVS_VERSIONS" not in source:
        raise RuntimeError("Unsupported vs_toolchain.py Visual Studio version table")

    header = WINDOWS_VS_VERSION_HEADER_RE.search(source, function.start(), function.end())
    if not header:
        raise RuntimeError("Unsupported vs_toolchain.py function indentation")
    body_indent = header.group("body_indent")
    newline = "\r\n" if "\r\n" in header.group("header") else "\n"
    body_start = header.start("body_indent")
    body_content_start = header.end()
    insert_at = body_start

    # Keep the function docstring as its first statement. Exact historical
    # templates use either a one-line or a body-indented multi-line docstring.
    quote_match = re.match(
        r"(?P<quote>'''|\"\"\")", source[body_content_start:]
    )
    if quote_match:
        quote = quote_match.group("quote")
        first_line_end = source.find("\n", body_content_start)
        if first_line_end < 0:
            first_line_end = len(source)
        first_line = source[body_content_start:first_line_end]
        if quote in first_line[len(quote) :]:
            insert_at = min(first_line_end + 1, len(source))
        else:
            closing = re.search(
                rf"(?m)^{re.escape(body_indent)}{re.escape(quote)}[ \t]*\r?$",
                source[first_line_end + 1 : function.end()],
            )
            if not closing:
                raise RuntimeError("Unsupported vs_toolchain.py function docstring")
            closing_end = first_line_end + 1 + closing.end()
            if source.startswith("\r\n", closing_end):
                insert_at = closing_end + 2
            elif source.startswith("\n", closing_end):
                insert_at = closing_end + 1
            else:
                insert_at = closing_end

    nested_indent = body_indent + ("\t" if "\t" in body_indent else "  ")
    injection = (
        f"{body_indent}{WINDOWS_VS_VERSION_MARKER}{newline}"
        f"{body_indent}jsc2js_msvs_version = "
        f"os.environ.get('GYP_MSVS_VERSION'){newline}"
        f"{body_indent}if jsc2js_msvs_version in MSVS_VERSIONS:{newline}"
        f"{nested_indent}return jsc2js_msvs_version{newline}"
    )
    return source[:insert_at] + injection + source[insert_at:]


def configure_windows_legacy_toolset(version: str, v8_root: Path = Path("v8")):
    """Select the installed MSVC headers compatible with historical clang-cl."""
    os.environ.pop("JSC2JS_VCVARS_VERSION", None)
    if not platform.system().lower().startswith("win"):
        return
    compatibility_year = windows_compatibility_year(version)
    vs_root = Path(os.environ.get("GYP_MSVS_OVERRIDE_PATH", ""))
    os.environ["GYP_MSVS_VERSION"] = compatibility_year
    os.environ[f"vs{compatibility_year}_install"] = str(vs_root)

    spec = windows_legacy_toolset_spec(version)
    if spec is None:
        return
    toolset_glob, toolset_name = spec

    toolsets_root = vs_root / "VC/Tools/MSVC"
    compatible = sorted(
        path for path in toolsets_root.glob(toolset_glob) if path.is_dir()
    )
    if not compatible:
        raise RuntimeError(f"MSVC {toolset_name} was not found under {toolsets_root}")
    vcvars_version = ".".join(compatible[-1].name.split(".")[:2])
    provide_legacy_vcvars_entry_point(vs_root)

    vs_toolchain = v8_root / "build/vs_toolchain.py"
    if not vs_toolchain.is_file():
        raise RuntimeError(f"Missing Windows VS toolchain helper: {vs_toolchain}")
    vs_source = vs_toolchain.read_text(encoding="utf-8")
    patched_vs_source = patch_windows_vs_toolchain_source(vs_source)
    if patched_vs_source != vs_source:
        vs_toolchain.write_text(patched_vs_source, encoding="utf-8")

    setup_toolchain = v8_root / "build/toolchain/win/setup_toolchain.py"
    if not setup_toolchain.is_file():
        raise RuntimeError(f"Missing Windows setup toolchain: {setup_toolchain}")
    source = setup_toolchain.read_text(encoding="utf-8")
    patched = patch_windows_setup_toolchain_source(source)
    if patched != source:
        setup_toolchain.write_text(patched, encoding="utf-8")
    os.environ["JSC2JS_VCVARS_VERSION"] = vcvars_version
    log(
        f"Using MSVC {toolset_name} ({compatible[-1].name}) through "
        f"{setup_toolchain} "
        f"for V8 {version}"
    )


def windows_linker_arg(v8_root: Path) -> str:
    """Use current MSVC link.exe when an old bundled lld cannot read its CRT."""
    candidates = (
        v8_root / "build/config/compiler/BUILD.gn",
        v8_root / "build/config/compiler/compiler.gni",
        v8_root / "build/toolchain/win/BUILD.gn",
    )
    if any(
        path.is_file()
        and "use_lld" in path.read_text(encoding="utf-8", errors="ignore")
        for path in candidates
    ):
        return "use_lld = false\n"
    return ""


def configure_windows_git_checkout():
    """Allow historical DEPS checkouts containing paths beyond MAX_PATH."""
    if platform.system().lower().startswith("win"):
        run("git config --global core.longpaths true", check=True)


def main():
    configure_python_compatibility()
    assigned_json = os.environ.get("ASSIGNED_JSON", "[]")
    apply_script = os.environ.get("APPLY_SCRIPT_NAME", "apply_patch.py")
    backup_base = Path(os.environ.get("BACKUP_BASE", "v8/out.gn/version_backups"))
    compress = os.environ.get("BACKUP_COMPRESS", "0") == "1"
    keep_work_dir = os.environ.get("KEEP_WORK_DIR", "0") == "1"
    skip_backup = os.environ.get("SKIP_BACKUP", "0") == "1"

    try:
        versions = json.loads(assigned_json)
        assert isinstance(versions, list)
    except Exception:
        log("ERROR: ASSIGNED_JSON invalid JSON list.")
        versions = []

    if not versions:
        write_list("success_versions.txt", [])
        write_list("failed_versions.txt", [])
        log("No versions to process.")
        return 0

    os_name = "Windows" if platform.system().lower().startswith("win") else "Linux"
    configure_windows_git_checkout()
    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(exist_ok=True)
    backup_base.mkdir(parents=True, exist_ok=True)

    success, failed = [], []

    for ver in versions:
        log(f"========== START {ver} ==========")
        restore_version_worktrees()
        run("git -C v8 reset --hard", check=False)
        for report_name in ("apply_patch_report.txt", "apply_patch_report.json"):
            (Path("v8") / report_name).unlink(missing_ok=True)
        sanitized = ver.replace(".", "_")
        try:
            if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", ver):
                raise RuntimeError(f"Invalid V8 tag: {ver!r}")
            # Ensure tag
            run(
                f"git -C v8 fetch --quiet --depth=1 origin "
                f"refs/tags/{ver}:refs/tags/{ver}",
                check=True,
            )
            run(f"git -C v8 checkout --detach {ver}", check=True)
            # Sync + hooks
            run("gclient sync -D --no-history --nohooks", check=True)
            hook_python = activate_legacy_hook_python(ver)
            if hook_python:
                patch_gclient_hook_dispatch(hook_python)
            in_tree_gyp = configure_in_tree_gyp(ver)
            if in_tree_gyp:
                work_dir = Path("v8/out/Release")
                if not keep_work_dir:
                    shutil.rmtree(work_dir, ignore_errors=True)
                activate_windows_vcvars(ver)
            else:
                configure_windows_legacy_toolset(ver)
            run("gclient runhooks", check=True)

            if not in_tree_gyp:
                work_dir = Path("v8/out.gn/x64.release")
                if not keep_work_dir:
                    shutil.rmtree(work_dir, ignore_errors=True)

            # Select a named stable patch, or one of the source-aware patchers.
            try:
                patch_strategy, selected = select_patch_implementation(ver)
                is_legacy = patch_strategy == "legacy-semantic"
                is_modern_semantic = patch_strategy == "modern-semantic"
                patch_file_to_use = (
                    selected if patch_strategy == "unified-diff" else None
                )
                log(f"Selected patch implementation for version {ver}: {selected}")
            
            except (ValueError, IndexError) as e:
                # Handle cases where version string is malformed (e.g., "12" or "a.b.c")
                log(f"[ERROR] Could not parse version string '{ver}': {e}. Defaulting to the current midrange patch")
                is_legacy = False
                is_modern_semantic = False
                patch_file_to_use = "patches/current/v8-12.6-to-13.2.134.patch"

            # Apply patch
            if is_legacy:
                legacy_patcher = Path("patches/legacy/apply_legacy_patch.py").resolve()
                if not legacy_patcher.exists():
                    raise RuntimeError(f"Missing legacy patcher {legacy_patcher}")
                rc = subprocess.run(
                    [
                        sys.executable,
                        str(legacy_patcher),
                        "--root",
                        ".",
                        "--report",
                        "apply_patch_report.json",
                    ],
                    cwd="v8",
                ).returncode
            elif is_modern_semantic:
                modern_patcher = Path("patches/modern/apply_modern_patch.py").resolve()
                if not modern_patcher.exists():
                    raise RuntimeError(f"Missing modern patcher {modern_patcher}")
                rc = subprocess.run(
                    [
                        sys.executable,
                        str(modern_patcher),
                        "--root",
                        ".",
                        "--report",
                        "apply_patch_report.json",
                    ],
                    cwd="v8",
                ).returncode
            else:
                apply_path = Path(apply_script).resolve()
                patch_path = Path(patch_file_to_use).resolve()
                if not apply_path.exists():
                    raise RuntimeError(f"Missing apply script {apply_script}")
                if not patch_path.exists():
                    raise RuntimeError(f"Missing patch file {patch_file_to_use}")
                rc = subprocess.run(
                    [
                        sys.executable,
                        str(apply_path),
                        "--patch",
                        str(patch_path),
                        "--verbose",
                        "--second-try-ignore-whitespace",
                        "--report",
                        "apply_patch_report.txt",
                    ],
                    cwd="v8",
                ).returncode
            if rc != 0:
                log(f"[PATCH] Failed for {ver}")
                failed.append(ver)
                collect_audit_records(
                    artifacts_dir,
                    ver,
                    os_name,
                    "Patch application returned a non-zero exit code.",
                )
                restore_version_worktrees()
                continue

            configure_host_compatibility()
            linux_legacy_gn_args = configure_v8_52_linux_gn(ver)
            if in_tree_gyp:
                # gclient runhooks generated this Ninja project before source
                # patching; the patch does not alter the build graph.
                run("ninja -C out/Release d8", cwd="v8", check=True)
            else:
                # v8gen.py is Python-2-only before V8 7.6. Its x64.release
                # preset is just release+x64, so generate the same args directly.
                work_dir.mkdir(parents=True, exist_ok=True)
                (work_dir / "args.gn").write_text(
                    'target_cpu = "x64"\n'
                    "is_debug = false\n"
                    "is_component_build = false\n"
                    "symbol_level = 0\n"
                    "v8_enable_disassembler = true\n"
                    + object_print_gn_arg(Path("v8"))
                    + linux_legacy_gn_args
                    + windows_warning_policy_gn_arg(Path("v8"))
                    + (windows_linker_arg(Path("v8")) if os_name == "Windows" else ""),
                    encoding="utf-8",
                    newline="\n",
                )
                run("gn gen out.gn/x64.release", cwd="v8", check=True)
                run("ninja -C out.gn/x64.release d8", cwd="v8", check=True)

            # --- FIX: Collect artifact (d8 AND snapshot_blob.bin) ---
            bin_name = "d8.exe" if os_name == "Windows" else "d8"
            build_output_dir = work_dir
            built_bin = build_output_dir / bin_name
            built_snapshot = build_output_dir / "snapshot_blob.bin"

            # FIX: Check for both files
            if not built_bin.exists() or not built_snapshot.exists():
                log(f"[BUILD] Missing binary or snapshot for {ver}. d8 exists: {built_bin.exists()}, snapshot exists: {built_snapshot.exists()}")
                failed.append(ver)
                collect_audit_records(
                    artifacts_dir,
                    ver,
                    os_name,
                    "Build output was incomplete: "
                    f"d8={built_bin.exists()} snapshot={built_snapshot.exists()}",
                )
                restore_version_worktrees()
                continue

            smoke_output = ""
            valid_cache_output = ""
            if is_legacy or is_modern_semantic:
                smoke_output = run_rejection_smoke(built_bin)
                valid_cache = valid_cache_for_version(ver)
                if valid_cache is not None:
                    valid_cache_output = run_valid_cache_smoke(
                        built_bin, valid_cache
                    )

            target_dir = collect_audit_records(artifacts_dir, ver, os_name)
            
            # FIX: Copy both files
            log(f"Copying {built_bin.name} and {built_snapshot.name} to {target_dir}")
            shutil.copy2(built_bin, target_dir / built_bin.name)
            shutil.copy2(built_snapshot, target_dir / built_snapshot.name)
            
            if smoke_output:
                (target_dir / "runtime_smoke.txt").write_text(
                    smoke_output, encoding="utf-8", newline="\n"
                )
            if valid_cache_output:
                (target_dir / "runtime_valid_cache_smoke.txt").write_text(
                    valid_cache_output, encoding="utf-8", newline="\n"
                )

            # Backup out.gn/x64.release
            if not skip_backup:
                backup_dir = backup_base / f"x64.release.{sanitized}"
                log(f"Backing up build directory to {backup_dir}")
                copytree(work_dir, backup_dir)

                if compress:
                    artifact = compress_backup(backup_dir)
                    log(f"Compressed backup: {artifact}")

            # Reset source modifications (keep backups + artifacts)
            restore_version_worktrees()

            success.append(ver)
            log(f"========== SUCCESS {ver} ==========")
        except Exception as e:
            log(f"[ERROR] {ver} failed: {e}")
            failed.append(ver)
            collect_audit_records(
                artifacts_dir,
                ver,
                os_name,
                traceback.format_exc(),
            )
            restore_version_worktrees()

    write_list("success_versions.txt", success)
    write_list("failed_versions.txt", failed)

    log("---- SUMMARY ----")
    log(f"Success: {success}")
    log(f"Failed : {failed}")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
