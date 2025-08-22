#!/usr/bin/env python3
import json, os, re, subprocess, sys

MIN_VERSION = os.environ.get("MIN_VERSION", "12.0.1").strip()
REPO_URL = os.environ.get("V8_REPO", "https://github.com/v8/v8.git")

# 每批最多处理多少版本（环境变量为空或非法时默认 20）
DEFAULT_CAP = 20
_raw_cap = os.environ.get("MAX_PER_RUN", "").strip()
try:
    CAP = int(_raw_cap) if _raw_cap else DEFAULT_CAP
    if CAP <= 0:
        CAP = DEFAULT_CAP
except ValueError:
    CAP = DEFAULT_CAP

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
OUTPUT = os.environ.get("GITHUB_OUTPUT")

def ver_tuple(v: str):
    return tuple(int(x) for x in v.split("."))

def main():
    print(f"[determine_versions] MIN_VERSION={MIN_VERSION} CAP(per batch)={CAP}")

    os.makedirs("public", exist_ok=True)
    processed_path = "public/version.json"
    if not os.path.exists(processed_path):
        with open(processed_path, "w", encoding="utf-8") as f:
            f.write("[]")
    try:
        with open(processed_path, "r", encoding="utf-8") as f:
            processed = json.load(f)
            if not isinstance(processed, list):
                processed = []
    except Exception:
        processed = []
    processed_set = set(processed)

    # 获取远程标签
    res = subprocess.run(
        ["git", "ls-remote", "--tags", REPO_URL],
        capture_output=True, text=True, check=True
    )
    tags_raw = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):]
        tag = tag.split("^")[0]
        if SEMVER_RE.match(tag):
            tags_raw.append(tag)

    tags = sorted(set(tags_raw), key=ver_tuple)
    min_t = ver_tuple(MIN_VERSION)
    unprocessed = [t for t in tags if ver_tuple(t) >= min_t and t not in processed_set]

    batch = unprocessed[:CAP]
    leftover_total = max(0, len(unprocessed) - len(batch))

    include = []
    for v in batch:
        include.append({"os": "ubuntu-latest", "version": v})
        include.append({"os": "windows-latest", "version": v})

    versions_json = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
    matrix_json = json.dumps({"include": include}, ensure_ascii=False, separators=(",", ":"))
    has_versions = "true" if batch else "false"

    print(f"Total new (unprocessed)={len(unprocessed)}, batch={len(batch)}, leftover_after_batch={leftover_total}")
    print("Batch versions:", batch)

    if OUTPUT:
        with open(OUTPUT, "a", encoding="utf-8") as out:
            out.write(f"versions={versions_json}\n")
            out.write(f"matrix={matrix_json}\n")
            out.write(f"has_versions={has_versions}\n")
            out.write(f"leftover_total={leftover_total}\n")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("determine_versions.py failed:", e, file=sys.stderr)
        sys.exit(1)
