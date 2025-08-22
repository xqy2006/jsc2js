#!/usr/bin/env python3
"""
Loop over assigned versions (JSON passed via ASSIGNED_JSON env),
for each:
  - git fetch tags + checkout tag
  - gclient sync + runhooks
  - apply patch
  - validate expected files changed
  - build into unique out.gn/x64.release.<version_with_underscores>
  - collect binary into artifacts/d8-<version>-<OS>/
Record success, failed, and failed reasons.

Exit code始终 0（让后续步骤继续统一归档），实际结果写入：
  success_versions.txt
  failed_versions.txt
  failed_reasons.txt (version<TAB>reason)
"""

import os, json, subprocess, shlex, sys, platform, shutil

ASSIGNED_JSON = os.environ.get("ASSIGNED_JSON", "[]")
try:
    assigned = json.loads(ASSIGNED_JSON)
    if not isinstance(assigned, list):
        raise ValueError
except Exception:
    print("[ERROR] ASSIGNED_JSON 不是有效 JSON 数组，内容=", ASSIGNED_JSON, file=sys.stderr)
    assigned = []

EXPECTED_FILES = {
    "src/d8/d8.cc",
    "src/d8/d8.h",
    "src/diagnostics/objects-printer.cc",
    "src/objects/string.cc",
    "src/snapshot/code-serializer.cc",
    "src/snapshot/deserializer.cc"
}

success = []
failed = []
failure_reasons = []  # (version, reason)

def run(cmd: str, cwd: str | None = None, reason: str | None = None, check: bool = True):
    print(f"[RUN] {cmd}")
    r = subprocess.run(cmd, cwd=cwd, shell=True)
    if check and r.returncode != 0:
        raise RuntimeError(reason or f"cmd-failed:{cmd}")
    return r.returncode

if not assigned:
    print("[INFO] 没有分配到版本。")
else:
    print("[INFO] 待处理版本列表:", assigned)

for v in assigned:
    print(f"\n==== Processing {v} ====")
    try:
        # 1. checkout tag
        try:
            run("git -C v8 fetch --tags", reason="fetch-tags")
            run(f"git -C v8 checkout {shlex.quote(v)}", reason="checkout-tag")
        except Exception as e:
            failed.append(v); failure_reasons.append((v, "checkout"))
            print(f"[FAIL-{v}] checkout: {e}")
            run("git -C v8 checkout .", check=False)
            continue

        # 2. sync deps
        try:
            run("gclient sync -D --no-history", reason="sync")
        except Exception as e:
            failed.append(v); failure_reasons.append((v, "sync"))
            print(f"[FAIL-{v}] sync: {e}")
            run("git -C v8 checkout .", check=False)
            continue

        # 3. runhooks
        try:
            run("gclient runhooks", reason="runhooks")
        except Exception as e:
            failed.append(v); failure_reasons.append((v, "runhooks"))
            print(f"[FAIL-{v}] runhooks: {e}")
            run("git -C v8 checkout .", check=False)
            continue

        # 4. apply patch
        rc = subprocess.run(
            "python3 apply_patch.py --patch patch.diff --verbose --report apply_patch_report.txt",
            cwd="v8", shell=True
        ).returncode
        if rc != 0:
            failed.append(v); failure_reasons.append((v, "patch"))
            print(f"[FAIL-{v}] patch rc={rc}")
            run("git -C v8 checkout .", check=False)
            continue

        # 5. diff 验证
        diff_out = subprocess.check_output("git -C v8 diff --name-only", shell=True, text=True)
        if not any(line.strip() in EXPECTED_FILES for line in diff_out.splitlines()):
            failed.append(v); failure_reasons.append((v, "patch-nochange"))
            print(f"[FAIL-{v}] patch-nochange")
            run("git -C v8 checkout .", check=False)
            continue

        # 6. build
        build_dir = f"out.gn/x64.release.{v.replace('.', '_')}"
        try:
            run(f"python tools/dev/v8gen.py {build_dir} -- v8_enable_disassembler=true v8_enable_object_print=true is_component_build=false",
                cwd="v8", reason="build-config")
        except Exception as e:
            failed.append(v); failure_reasons.append((v, "build-config"))
            print(f"[FAIL-{v}] build-config: {e}")
            run("git -C v8 checkout .", check=False)
            continue

        try:
            run(f"ninja -C {build_dir} d8", cwd="v8", reason="build-ninja")
        except Exception as e:
            failed.append(v); failure_reasons.append((v, "build-ninja"))
            print(f"[FAIL-{v}] build-ninja: {e}")
            run("git -C v8 checkout .", check=False)
            continue

        # 7. 收集产物
        os_name = "Windows" if platform.system().lower().startswith("win") else "Linux"
        artifacts_root = "artifacts"
        os.makedirs(artifacts_root, exist_ok=True)
        target_dir = os.path.join(artifacts_root, f"d8-{v}-{os_name}")
        os.makedirs(target_dir, exist_ok=True)

        binary_name = "d8.exe" if os_name == "Windows" else "d8"
        src_bin = os.path.join("v8", build_dir, binary_name)
        if not os.path.exists(src_bin):
            failed.append(v); failure_reasons.append((v, "binary-missing"))
            print(f"[FAIL-{v}] binary-missing")
            run("git -C v8 checkout .", check=False)
            continue

        shutil.copy2(src_bin, os.path.join(target_dir, binary_name))
        report_src = os.path.join("v8", "apply_patch_report.txt")
        if os.path.exists(report_src):
            shutil.copy2(report_src, os.path.join(target_dir, "apply_patch_report.txt"))

        # 8. 清理修改，以便下一个版本 clean
        run("git -C v8 checkout .", check=False)
        success.append(v)
        print(f"[OK] {v}")

    except Exception as e:
        failed.append(v); failure_reasons.append((v, "exception"))
        print(f"[FAIL-{v}] exception: {e}")
        run("git -C v8 checkout .", check=False)

print("\n====== SUMMARY ======")
print("SUCCESS:", success)
print("FAILED :", failed)

with open("success_versions.txt", "w", encoding="utf-8") as f:
    for s in success:
        f.write(s + "\n")
with open("failed_versions.txt", "w", encoding="utf-8") as f:
    for s in failed:
        f.write(s + "\n")
with open("failed_reasons.txt", "w", encoding="utf-8") as f:
    for v, r in failure_reasons:
        f.write(f"{v}\t{r}\n")
