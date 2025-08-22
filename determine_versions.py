#!/usr/bin/env python3
import json, os, re, subprocess, sys

MIN_VERSION = os.environ.get("MIN_VERSION", "12.0.1").strip()
MAX_PER_RUN = os.environ.get("MAX_PER_RUN", "").strip()
REPO_URL = os.environ["V8_REPO"]

OUTPUT = os.environ.get("GITHUB_OUTPUT")

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

def ver_tuple(v: str):
    return tuple(int(x) for x in v.split("."))

def main():
    # 读取已处理版本
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

    # 获取所有标签
    res = subprocess.run(
        ["git", "ls-remote", "--tags", REPO_URL],
        capture_output=True, text=True, check=True
    )
    tag_lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]

    tags = []
    for line in tag_lines:
        parts = line.split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):]
        tag = tag.split("^")[0]  # 去除 ^{}
        if SEMVER_RE.match(tag):
            tags.append(tag)

    # 去重 + 排序
    tags = sorted(set(tags), key=ver_tuple)

    min_t = ver_tuple(MIN_VERSION)
    new_versions = [t for t in tags if ver_tuple(t) >= min_t and t not in processed_set]

    if MAX_PER_RUN:
        try:
            limit = int(MAX_PER_RUN)
            if limit > 0:
                new_versions = new_versions[:limit]
        except ValueError:
            pass

    include = []
    for v in new_versions:
        include.append({"os": "ubuntu-latest", "version": v})
        include.append({"os": "windows-latest", "version": v})

    versions_json = json.dumps(new_versions, ensure_ascii=False, separators=(",", ":"))
    matrix_json = json.dumps({"include": include}, ensure_ascii=False, separators=(",", ":"))
    has_versions = "true" if new_versions else "false"

    print("Detected new versions:", new_versions)

    if OUTPUT:
        with open(OUTPUT, "a", encoding="utf-8") as out:
            out.write(f"versions={versions_json}\n")
            out.write(f"matrix={matrix_json}\n")
            out.write(f"has_versions={has_versions}\n")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("determine_versions.py failed:", e, file=sys.stderr)
        # 不抛出堆栈以免破坏 YAML，可根据需要改为 sys.exit(1)
        sys.exit(1)
