#!/usr/bin/env python3
"""
Use patched sources from Linux artifacts to build Windows d8.

Process:
  - Read linux_artifacts/success_versions.txt
  - For each version:
      * git fetch --tags && checkout tag
      * gclient sync (shallow ok)
      * overlay artifacts/patched-src-<version> files into v8 working tree
      * v8gen + ninja d8.exe
      * store binary + (optionally) copied apply_patch_report.txt from Linux artifact
Outputs:
  win_artifacts/d8-<ver>-Windows/d8.exe
  win_success_versions.txt
  win_failed_versions.txt
"""
import os, subprocess, shutil, sys, platform
from pathlib import Path
from datetime import datetime

EXPECTED_FILES = [
    "src/d8/d8.cc",
    "src/d8/d8.h",
    "src/diagnostics/objects-printer.cc",
    "src/objects/string.cc",
    "src/snapshot/code-serializer.cc",
    "src/snapshot/deserializer.cc",
]

def log(msg): print(f"[{datetime.utcnow().isoformat()}] {msg}")

def run(cmd, cwd=None, check=True):
    log(f"RUN: {cmd}")
    r = subprocess.run(cmd, cwd=cwd, shell=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed {r.returncode}: {cmd}")
    return r.returncode

def main():
    if not platform.system().lower().startswith("win"):
        log("This script is intended for Windows.")
        return 1

    linux_root = Path(os.environ.get("LINUX_ARTIFACT_ROOT", "linux_artifacts"))
    success_file = linux_root / "success_versions.txt"
    if not success_file.exists():
        log("No success_versions.txt from Linux – nothing to build.")
        Path("win_success_versions.txt").write_text("")
        Path("win_failed_versions.txt").write_text("")
        return 0

    versions = [v.strip() for v in success_file.read_text().splitlines() if v.strip()]
    win_success, win_failed = [], []
    out_dir = Path("win_artifacts")
    out_dir.mkdir(exist_ok=True)
    v8_root = Path("v8")

    for ver in versions:
        log(f"========== START WIN {ver} ==========")
        try:
            run("git -C v8 fetch --tags --quiet", check=True)
            run(f"git -C v8 checkout {ver}", check=True)
            run("gclient sync -D --nohooks --no-history", check=True)
            run("gclient runhooks", check=True)

            # overlay sources
            patched_src_dir = linux_root / f"patched-src-{ver}"
            if not patched_src_dir.exists():
                log(f"[OVERLAY] Missing patched-src-{ver}, mark failed.")
                win_failed.append(ver)
                run("git -C v8 checkout .", check=False)
                continue
            # copy each expected file
            for rel in EXPECTED_FILES:
                src = patched_src_dir / rel
                if not src.is_file():
                    log(f"[OVERLAY] Missing file {rel} in patched-src-{ver}")
                    # 允许继续，但标记风险
                dest = v8_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_file():
                    shutil.copy2(src, dest)

            # build
            gn_args = "v8_enable_disassembler=true v8_enable_object_print=true is_component_build=false is_debug=false"
            run("python tools/dev/v8gen.py x64.release -- " + gn_args, cwd="v8", check=True)
            run("ninja -C out.gn/x64.release d8", cwd="v8", check=True)

            bin_path = v8_root / "out.gn/x64.release/d8.exe"
            if not bin_path.exists():
                log("[BUILD] Missing d8.exe after build.")
                win_failed.append(ver)
                run("git -C v8 checkout .", check=False)
                continue

            tgt = out_dir / f"d8-{ver}-Windows"
            tgt.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bin_path, tgt / "d8.exe")

            # 复制 Linux 的 apply_patch_report（可选）
            l_report = linux_root / f"d8-{ver}-Linux" / "apply_patch_report.txt"
            if l_report.exists():
                shutil.copy2(l_report, tgt / "apply_patch_report.txt")

            run("git -C v8 checkout .", check=False)
            win_success.append(ver)
            log(f"========== SUCCESS WIN {ver} ==========")
        except Exception as e:
            log(f"[ERROR] WIN {ver} failed: {e}")
            win_failed.append(ver)
            run("git -C v8 checkout .", check=False)

    Path("win_success_versions.txt").write_text("\n".join(win_success) + ("\n" if win_success else ""))
    Path("win_failed_versions.txt").write_text("\n".join(win_failed) + ("\n" if win_failed else ""))

    log("---- WINDOWS SUMMARY ----")
    log(f"Success: {win_success}")
    log(f"Failed : {win_failed}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
