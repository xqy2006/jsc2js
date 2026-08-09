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
import json, os, platform, shutil, subprocess, sys
from pathlib import Path
from datetime import datetime
import re

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
    return {l.strip() for l in out.splitlines() if l.strip()}

def write_list(path: str, items):
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(it + "\n")

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


def run_legacy_rejection_smoke(built_bin: Path) -> str:
    """Verify malformed cache data is rejected without aborting the process."""
    build_dir = built_bin.parent.resolve()
    bad_cache = build_dir / "jsc2js-invalid-cache.jsc"
    bad_cache.write_bytes(b"not-a-v8-code-cache\0" + bytes(64))
    marker = "JSC2JS_SAFE_REJECTION"
    javascript = (
        "try { loadjsc('jsc2js-invalid-cache.jsc'); "
        "print('JSC2JS_UNEXPECTED_ACCEPT'); quit(3); } "
        f"catch (error) {{ print('{marker}:' + error); }}"
    )
    completed = subprocess.run(
        [str(built_bin.resolve()), "-e", javascript],
        cwd=str(build_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    output = completed.stdout or ""
    if completed.returncode != 0 or marker not in output:
        raise RuntimeError(
            "legacy malformed-cache smoke test failed: "
            f"exit={completed.returncode} output={output[-2000:]}"
        )
    return output


def run_legacy_valid_cache_smoke(built_bin: Path, cache_path: Path) -> str:
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
        [str(built_bin.resolve()), "--no-lazy", "-e", javascript],
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
            "legacy valid-cache smoke test failed: "
            f"exit={completed.returncode} output={output[-4000:]}"
        )
    return output


def configure_host_compatibility():
    """Make historical LLVM binaries usable on current Linux runners."""
    if not platform.system().lower().startswith("linux"):
        return
    try:
        system_libstdcpp = subprocess.check_output(
            ["g++", "-print-file-name=libstdc++.so.6"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return
    if not system_libstdcpp or system_libstdcpp == "libstdc++.so.6":
        return
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
    major = int(version.split(".", 1)[0])
    if major == 5:
        return "14.29.*", "v142"
    if major < 8:
        return "14.16.*", "v141"
    if major < 10:
        return "14.29.*", "v142"
    return None


def uses_in_tree_gyp(version: str) -> bool:
    """V8 5.1 predates the standalone Chromium //build GN dependency."""
    major, minor = (int(part) for part in version.split(".", 2)[:2])
    return (major, minor) == (5, 1)


def configure_in_tree_gyp(version: str) -> bool:
    """Select V8 5.1's native GYP/Ninja generator, or clear stale batch state."""
    if not uses_in_tree_gyp(version):
        for name in ("GYP_GENERATORS", "GYP_GENERATOR_FLAGS", "GYP_DEFINES"):
            os.environ.pop(name, None)
        return False

    os.environ["GYP_GENERATORS"] = "ninja"
    os.environ["GYP_GENERATOR_FLAGS"] = "output_dir=out"
    os.environ["GYP_DEFINES"] = " ".join(
        (
            "target_arch=x64",
            "v8_target_arch=x64",
            "v8_enable_disassembler=1",
            "v8_object_print=1",
            "v8_use_external_startup_data=1",
            "component=static_library",
        )
    )
    log(f"Using the in-tree GYP/Ninja generator for V8 {version}")
    return True


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
    if not vcvars.is_file():
        raise RuntimeError(f"vcvarsall.bat was not found at {vcvars}")
    sdk_version = os.environ.get("JSC2JS_WINDOWS_SDK_VERSION", "")
    sdk_option = f" -winsdk={sdk_version}" if sdk_version else ""
    command = (
        f'call "{vcvars}" amd64 -vcvars_ver={vcvars_version}'
        f"{sdk_option} >nul && set"
    )
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", command],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in completed.stdout.splitlines():
        if "=" not in line or line.startswith("="):
            continue
        name, value = line.split("=", 1)
        os.environ[name] = value
    os.environ["GYP_MSVS_VERSION"] = "2015"
    os.environ["GYP_MSVS_OVERRIDE_PATH"] = str(vs_root)
    log(
        f"Imported MSVC {toolset_name} ({compatible[-1].name}) and SDK "
        f"{sdk_version or 'default'} for V8 {version} GYP"
    )


def configure_windows_legacy_toolset(version: str, v8_root: Path = Path("v8")):
    """Select the installed MSVC headers compatible with historical clang-cl."""
    os.environ.pop("JSC2JS_VCVARS_VERSION", None)
    if not platform.system().lower().startswith("win"):
        return
    major = int(version.split(".", 1)[0])
    if major == 5:
        compatibility_year = "2015"
    elif major == 6:
        compatibility_year = "2017"
    elif major < 10:
        compatibility_year = "2019"
    else:
        compatibility_year = "2022"
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

    setup_toolchain = v8_root / "build/toolchain/win/setup_toolchain.py"
    if not setup_toolchain.is_file():
        raise RuntimeError(f"Missing Windows setup toolchain: {setup_toolchain}")
    marker = "# JSC2JS_LEGACY_VCVARS_VERSION"
    sdk_marker = "# JSC2JS_INSTALLED_WINDOWS_SDK"
    atlmfc_marker = "# JSC2JS_OPTIONAL_ATLMFC"
    source = setup_toolchain.read_text(encoding="utf-8")
    modified = False
    if marker not in source:
        match = WINDOWS_TOOLCHAIN_ARGS_RE.search(source)
        if not match:
            raise RuntimeError(
                f"Unsupported setup_toolchain.py layout in {setup_toolchain}"
            )
        indent = match.group("indent")
        injection = (
            match.group(0)
            + f"\n{indent}{marker}\n"
            + f"{indent}jsc2js_vcvars_version = "
            + "os.environ.get('JSC2JS_VCVARS_VERSION')\n"
            + f"{indent}if jsc2js_vcvars_version:\n"
            + f"{indent}  args.append('-vcvars_ver=' + jsc2js_vcvars_version)"
        )
        source = source[: match.start()] + injection + source[match.end() :]
        modified = True
    if sdk_marker not in source:
        matches = list(WINDOWS_TOOLCHAIN_ENV_RE.finditer(source))
        if len(matches) != 1:
            raise RuntimeError(
                "Unsupported setup_toolchain.py SDK environment layout in "
                f"{setup_toolchain}: found {len(matches)} anchors"
            )
        match = matches[0]
        indent = match.group("indent")
        injection = (
            f"{indent}{sdk_marker}\n"
            f"{indent}jsc2js_sdk_version = "
            "os.environ.get('JSC2JS_WINDOWS_SDK_VERSION')\n"
            f"{indent}if jsc2js_sdk_version:\n"
            f"{indent}  args = [arg for arg in args if not (\n"
            f"{indent}      isinstance(arg, str) and arg.count('.') == 3 and\n"
            f"{indent}      all(part.isdigit() for part in arg.split('.')))]\n"
            f"{indent}  args.append(jsc2js_sdk_version)\n"
            + match.group(0)
        )
        source = source[: match.start()] + injection + source[match.end() :]
        modified = True
    if atlmfc_marker not in source and "assert vc_lib_atlmfc_path" in source:
        source = source.replace(
            "assert vc_lib_atlmfc_path",
            f"pass  {atlmfc_marker}: d8 does not link the optional ATL/MFC libraries",
            1,
        )
        modified = True
    if modified:
        setup_toolchain.write_text(source, encoding="utf-8")
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
    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(exist_ok=True)
    backup_base.mkdir(parents=True, exist_ok=True)

    success, failed = [], []

    for ver in versions:
        log(f"========== START {ver} ==========")
        run("git -C v8 reset --hard", check=False)
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

            # Select a named V8 12+ patch, or the source-aware legacy patcher.
            try:
                # Split version string into parts and convert major/minor to integers
                version_parts = ver.split('.')
                major = int(version_parts[0])
                minor = int(version_parts[1])
                minor_2 = int(version_parts[2])
                is_legacy = major < 12
                if is_legacy:
                    patch_file_to_use = None
                elif major > 12 or (major == 12 and minor >= 6):
                    if major > 13 or (major == 13 and minor > 2) or (major == 13 and minor == 2 and minor_2 >= 135):
                        patch_file_to_use = "patches/current/v8-13.2.135-plus.patch"
                    else:  
                        patch_file_to_use = "patches/current/v8-12.6-to-13.2.134.patch"
                else:
                    patch_file_to_use = "patches/current/v8-12.0-to-12.5.patch"
                selected = patch_file_to_use or "patches/legacy/apply_legacy_patch.py"
                log(f"Selected patch implementation for version {ver}: {selected}")
            
            except (ValueError, IndexError) as e:
                # Handle cases where version string is malformed (e.g., "12" or "a.b.c")
                log(f"[ERROR] Could not parse version string '{ver}': {e}. Defaulting to the current midrange patch")
                is_legacy = False
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
                run("git -C v8 checkout .", check=False)
                continue

            configure_host_compatibility()
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
                    "v8_enable_object_print = true\n"
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
                run("git -C v8 checkout .", check=False)
                continue

            smoke_output = ""
            valid_cache_output = ""
            if is_legacy:
                smoke_output = run_legacy_rejection_smoke(built_bin)
                valid_cache = os.environ.get("JSC2JS_VALID_CACHE")
                if valid_cache:
                    valid_cache_output = run_legacy_valid_cache_smoke(
                        built_bin, Path(valid_cache).resolve()
                    )

            target_dir = artifacts_dir / f"d8-{ver}-{os_name}"
            target_dir.mkdir(parents=True, exist_ok=True)
            
            # FIX: Copy both files
            log(f"Copying {built_bin.name} and {built_snapshot.name} to {target_dir}")
            shutil.copy2(built_bin, target_dir / built_bin.name)
            shutil.copy2(built_snapshot, target_dir / built_snapshot.name)
            
            report_file = Path("v8/apply_patch_report.txt")
            if report_file.exists():
                shutil.copy2(report_file, target_dir / "apply_patch_report.txt")
            legacy_report = Path("v8/apply_patch_report.json")
            if legacy_report.exists():
                shutil.copy2(legacy_report, target_dir / "apply_patch_report.json")
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
            run("git -C v8 checkout .", check=False)

            success.append(ver)
            log(f"========== SUCCESS {ver} ==========")
        except Exception as e:
            log(f"[ERROR] {ver} failed: {e}")
            failed.append(ver)
            run("git -C v8 checkout .", check=False)

    write_list("success_versions.txt", success)
    write_list("failed_versions.txt", failed)

    log("---- SUMMARY ----")
    log(f"Success: {success}")
    log(f"Failed : {failed}")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
