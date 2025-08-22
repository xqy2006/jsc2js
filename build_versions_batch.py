#!/usr/bin/env python3
"""
Batch build script for a list of V8 versions (single OS slot).

Environment Inputs:
  ASSIGNED_JSON         JSON array of version strings to build.
  PATCH_FILE_NAME       (optional) patch filename inside v8 dir (default: patch.diff)
  APPLY_SCRIPT_NAME     (optional) patch applier script inside v8 dir (default: apply_patch.py)

Behavior:
  For each version:
    - git fetch --tags (once per version to ensure tag availability)
    - git checkout <version>
    - gclient sync -D --no-history
    - gclient runhooks
    - Clean build dir out.gn/x64.release.<ver_underscored>
    - Apply patch (python3 apply_patch.py --patch patch.diff)
    - Verify expected files changed
    - Configure + build d8
    - Collect artifact into artifacts/d8-<version>-<OS>/
    - Revert changes for next iteration

Outputs:
  success_versions.txt
  failed_versions.txt

The step never exits non-zero on per-version failures; it aggregates them.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

EXPECTED_FILES = {
    "src/d8/d8.cc",
    "src/d8/d8.h",
    "src/diagnostics/objects-printer.cc",
    "src/objects/string.cc",
    "src/snapshot/code-serializer.cc",
    "src/snapshot/deserializer.cc",
}

def run(cmd: str, cwd: str = None, check: bool = True) -> int:
    print(f"[RUN] {cmd}")
    result = subprocess.run(cmd, cwd=cwd, shell=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {cmd}")
    return result.returncode

def git_diff_files() -> set:
    out = subprocess.check_output("git -C v8 diff --name-only", shell=True, text=True)
    return {line.strip() for line in out.splitlines() if line.strip()}

def main():
    assigned_json = os.environ.get("ASSIGNED_JSON", "[]")
    patch_file = os.environ.get("PATCH_FILE_NAME", "patch.diff")
    apply_script = os.environ.get("APPLY_SCRIPT_NAME", "apply_patch.py")

    try:
        versions = json.loads(assigned_json)
        assert isinstance(versions, list)
    except Exception:
        print("[ERROR] ASSIGNED_JSON is invalid JSON list.")
        versions = []

    if not versions:
        print("[INFO] No versions assigned.")
        Path("success_versions.txt").write_text("", encoding="utf-8")
        Path("failed_versions.txt").write_text("", encoding="utf-8")
        return 0

    os_name = "Windows" if platform.system().lower().startswith("win") else "Linux"
    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(exist_ok=True)

    success = []
    failed = []

    for ver in versions:
        print(f"\n========== Processing version {ver} ==========")
        try:
            # Ensure we have fresh tag info
            run("git -C v8 fetch --tags", check=True)

            # Checkout tag
            run(f"git -C v8 checkout {ver}", check=True)

            # Sync dependencies for this tag
            # (Run from root where .gclient exists)
            run("gclient sync -D --no-history", check=True)
            run("gclient runhooks", check=True)

            # Clean build dir
            build_dir = f"out.gn/x64.release.{ver.replace('.', '_')}"
            run(f"rm -rf v8/{build_dir}", check=False)

            # Apply patch
            if not Path(f"v8/{apply_script}").exists():
                raise RuntimeError(f"Missing patch applier script: {apply_script}")
            if not Path(f"v8/{patch_file}").exists():
                raise RuntimeError(f"Missing patch file: {patch_file}")

            patch_cmd = f"python3 {apply_script} --patch {patch_file} --verbose --report apply_patch_report.txt"
            rc = subprocess.run(patch_cmd, cwd="v8", shell=True).returncode
            if rc != 0:
                print(f"[PATCH] Patch apply failed (rc={rc}) for {ver}")
                failed.append(ver)
                # Revert modifications
                run("git -C v8 checkout .", check=False)
                continue

            # Verify expected modifications
            changed = git_diff_files()
            if not (EXPECTED_FILES & changed):
                print(f"[PATCH] No expected tracked file changed; treat as failure for {ver}")
                failed.append(ver)
                run("git -C v8 checkout .", check=False)
                continue

            # Configure & build
            gn_args = "v8_enable_disassembler=true v8_enable_object_print=true is_component_build=false"
            run(f"python tools/dev/v8gen.py {build_dir} -- {gn_args}", cwd="v8", check=True)
            run(f"ninja -C {build_dir} d8", cwd="v8", check=True)

            # Collect artifact
            bin_name = "d8.exe" if os_name == "Windows" else "d8"
            built_bin = Path("v8") / build_dir / bin_name
            if not built_bin.exists():
                print(f"[BUILD] Missing binary after build for {ver}")
                failed.append(ver)
                run("git -C v8 checkout .", check=False)
                continue

            target_dir = artifacts_dir / f"d8-{ver}-{os_name}"
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(built_bin, target_dir / bin_name)

            report_file = Path("v8") / "apply_patch_report.txt"
            if report_file.exists():
                shutil.copy2(report_file, target_dir / "apply_patch_report.txt")

            # Clean modifications (keep built outputs)
            run("git -C v8 checkout .", check=False)

            success.append(ver)
            print(f"========== Version {ver} SUCCESS ==========")
        except Exception as e:
            print(f"[ERROR] Version {ver} failed: {e}")
            failed.append(ver)
            run("git -C v8 checkout .", check=False)

    # Write summary files
    with open("success_versions.txt", "w", encoding="utf-8") as f:
        for v in success:
            f.write(v + "\n")
    with open("failed_versions.txt", "w", encoding="utf-8") as f:
        for v in failed:
            f.write(v + "\n")

    print("\n---- Summary ----")
    print("Success:", success)
    print("Failed :", failed)
    # Always return 0 so workflow can aggregate; failures are tracked in file.
    return 0

if __name__ == "__main__":
    sys.exit(main())
