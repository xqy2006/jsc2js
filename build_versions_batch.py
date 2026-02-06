#!/usr/bin/env python3
"""
Batch build script (v8gen-only + per-version backup of out.gn/x64.release).

For each version:
  - fetch tags, checkout tag
  - gclient sync -D --no-history && gclient runhooks
  - remove existing v8/out.gn/x64.release (unless KEEP_WORK_DIR=1)
  - run: python tools/dev/v8gen.py x64.release -- <args>
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
"""
import json, os, platform, shutil, subprocess, sys
from pathlib import Path
from datetime import datetime

EXPECTED_FILES = {
    "src/d8/d8.cc",
    "src/d8/d8.h",
    "src/diagnostics/objects-printer.cc",
    "src/objects/string.cc",
    "src/snapshot/code-serializer.cc",
    "src/snapshot/deserializer.cc",
}

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

def main():
    assigned_json = os.environ.get("ASSIGNED_JSON", "[]")
    apply_script = os.environ.get("APPLY_SCRIPT_NAME", "apply_patch.py")
    backup_base = Path(os.environ.get("BACKUP_BASE", "v8/out.gn/version_backups"))
    compress = os.environ.get("BACKUP_COMPRESS", "0") == "1"
    keep_work_dir = os.environ.get("KEEP_WORK_DIR", "0") == "1"

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
            # Ensure tag
            run("git -C v8 fetch --tags --quiet", check=True)
            run(f"git -C v8 checkout {ver}", check=True)
            # Sync + hooks
            run("gclient sync -D --no-history", check=True)
            run("gclient runhooks", check=True)

            work_dir = Path("v8/out.gn/x64.release")
            if not keep_work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)

            # --- MODIFICATION START: Dynamically select patch file ---
            try:
                # Split version string into parts and convert major/minor to integers
                version_parts = ver.split('.')
                major = int(version_parts[0])
                minor = int(version_parts[1])
                minor_2 = int(version_parts[2])
                # Check if version is greater than or equal to 12.6
                if major > 12 or (major == 12 and minor >= 6):
                    if major > 13 or (major == 13 and minor > 2) or (major == 13 and minor == 2 and minor_2 >= 135):
                        patch_file_to_use = "patch_1_v2.diff"
                    else:  
                        patch_file_to_use = "patch_v2.diff"
                else:
                    patch_file_to_use = "patch_old_v2.diff"
                log(f"Selected patch file for version {ver}: {patch_file_to_use}")
            
            except (ValueError, IndexError) as e:
                # Handle cases where version string is malformed (e.g., "12" or "a.b.c")
                log(f"[ERROR] Could not parse version string '{ver}': {e}. Defaulting to patch.diff")
                patch_file_to_use = "patch.diff"
            # --- MODIFICATION END ---

            # Apply patch
            apply_path = Path("v8") / apply_script
            patch_path = Path("v8") / patch_file_to_use
            if not apply_path.exists():
                raise RuntimeError(f"Missing apply script {apply_script}")
            if not patch_path.exists():
                raise RuntimeError(f"Missing patch file {patch_file_to_use}")

            rc = subprocess.run(
                f"python3 {apply_script} --patch {patch_file_to_use} --verbose --second-try-ignore-whitespace --report apply_patch_report.txt",
                cwd="v8", shell=True).returncode
            if rc != 0:
                log(f"[PATCH] Failed for {ver}")
                failed.append(ver)
                run("git -C v8 checkout .", check=False)
                continue

            # v8gen config
            gn_args = "v8_enable_disassembler=true v8_enable_object_print=true is_component_build=false is_debug=false"
            run(f"python tools/dev/v8gen.py x64.release --vv -- {gn_args}", cwd="v8", check=True)

            # Build
            run("ninja -C out.gn/x64.release d8", cwd="v8", check=True)

            # --- FIX: Collect artifact (d8 AND snapshot_blob.bin) ---
            bin_name = "d8.exe" if os_name == "Windows" else "d8"
            build_output_dir = Path("v8/out.gn/x64.release")
            built_bin = build_output_dir / bin_name
            built_snapshot = build_output_dir / "snapshot_blob.bin"

            # FIX: Check for both files
            if not built_bin.exists() or not built_snapshot.exists():
                log(f"[BUILD] Missing binary or snapshot for {ver}. d8 exists: {built_bin.exists()}, snapshot exists: {built_snapshot.exists()}")
                failed.append(ver)
                run("git -C v8 checkout .", check=False)
                continue

            target_dir = artifacts_dir / f"d8-{ver}-{os_name}"
            target_dir.mkdir(parents=True, exist_ok=True)
            
            # FIX: Copy both files
            log(f"Copying {built_bin.name} and {built_snapshot.name} to {target_dir}")
            shutil.copy2(built_bin, target_dir / built_bin.name)
            shutil.copy2(built_snapshot, target_dir / built_snapshot.name)
            
            report_file = Path("v8/apply_patch_report.txt")
            if report_file.exists():
                shutil.copy2(report_file, target_dir / "apply_patch_report.txt")

            # Backup out.gn/x64.release
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
    return 0

if __name__ == "__main__":
    sys.exit(main())
