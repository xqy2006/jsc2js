#!/usr/bin/env python3
import json, os, platform, shutil, subprocess, sys, hashlib
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

def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}")

def run(cmd, cwd=None, check=True):
    log(f"RUN: {cmd}")
    r = subprocess.run(cmd, cwd=cwd, shell=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {cmd}")
    return r.returncode

def read_json_env(name, default):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return val
    except:
        pass
    return default

def file_sha(path: Path):
    if not path.is_file():
        return None
    h = hashlib.sha1()
    with path.open("rb") as f:
        h.update(f.read())
    return h.hexdigest()

def snapshot(root: Path, rels):
    return {r: file_sha(root / r) for r in rels}

def changed_after(root: Path, before: dict):
    out = []
    for rel, sha in before.items():
        new_sha = file_sha(root / rel)
        if new_sha != sha:
            out.append(rel)
    return out

def copy_expected(root: Path, version: str, out_dir: Path):
    tar_dir = out_dir / f"patched-src-{version}"
    tar_dir.mkdir(parents=True, exist_ok=True)
    for rel in EXPECTED_FILES:
        src = root / rel
        if src.is_file():
            target = tar_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    return tar_dir

def main():
    versions = read_json_env("ASSIGNED_JSON", [])
    if not versions:
        Path("success_versions.txt").write_text("")
        Path("failed_versions.txt").write_text("")
        log("No versions to process.")
        return 0

    is_windows = platform.system().lower().startswith("win")
    if is_windows:
        log("This script is intended for Linux only in this workflow.")
        return 1

    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(exist_ok=True)

    success, failed = [], []
    v8_root = Path("v8")

    for ver in versions:
        log(f"========== START {ver} ==========")
        try:
            run("git -C v8 fetch --tags --quiet", check=True)
            run(f"git -C v8 checkout {ver}", check=True)

            # 加强：确保至少一定深度，降低 3-way 失败
            run("git -C v8 fetch --deepen=200 || true", check=False)

            run("gclient sync -D --nohooks", check=True)
            run("gclient runhooks", check=True)

            before = snapshot(v8_root, EXPECTED_FILES)

            # Apply patch
            rc = subprocess.run(
                "python3 apply_patch.py --patch patch.diff --report apply_patch_report.txt",
                cwd="v8", shell=True).returncode
            if rc != 0:
                log(f"[PATCH] apply_patch.py rc={rc}")
                failed.append(ver)
                run("git -C v8 checkout .", check=False)
                continue

            after_changed = changed_after(v8_root, before)
            if not after_changed:
                log(f"[PATCH] No actual diff for {ver} (already integrated?) Mark success-noop.")
            else:
                log(f"[PATCH] Modified files: {after_changed}")

            # v8gen + build
            gn_args = "v8_enable_disassembler=true v8_enable_object_print=true is_component_build=false is_debug=false"
            run("python tools/dev/v8gen.py x64.release -- " + gn_args, cwd="v8", check=True)
            run("ninja -C out.gn/x64.release d8", cwd="v8", check=True)

            bin_path = v8_root / "out.gn/x64.release/d8"
            if not bin_path.exists():
                log("[BUILD] Missing Linux d8")
                failed.append(ver)
                run("git -C v8 checkout .", check=False)
                continue

            target_dir = artifacts_dir / f"d8-{ver}-Linux"
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bin_path, target_dir / "d8")
            rep = v8_root / "apply_patch_report.txt"
            if rep.exists():
                shutil.copy2(rep, target_dir / "apply_patch_report.txt")

            # 导出 6 文件
            copy_expected(v8_root, ver, artifacts_dir)

            # reset (保留 artifacts)
            run("git -C v8 checkout .", check=False)

            success.append(ver)
            log(f"========== SUCCESS {ver} ==========")
        except Exception as e:
            log(f"[ERROR] {ver} failed: {e}")
            failed.append(ver)
            run("git -C v8 checkout .", check=False)

    Path("success_versions.txt").write_text("\n".join(success) + ("\n" if success else ""))
    Path("failed_versions.txt").write_text("\n".join(failed) + ("\n" if failed else ""))

    log("---- SUMMARY ----")
    log(f"Success: {success}")
    log(f"Failed : {failed}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
