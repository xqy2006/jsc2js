#!/usr/bin/env python3
import json, os, re, subprocess, sys
from typing import List

MIN_VERSION = os.environ.get("MIN_VERSION", "12.0.1").strip()
REPO_URL = os.environ.get("V8_REPO", "https://chromium.googlesource.com/v8/v8.git")
DEFAULT_CAP = 20
_raw_cap = os.environ.get("MAX_PER_RUN", "").strip()
try:
    CAP = int(_raw_cap) if _raw_cap else DEFAULT_CAP
    if CAP <= 0:
        CAP = DEFAULT_CAP
except ValueError:
    CAP = DEFAULT_CAP

# 允许 3 或 4 段：12.0.1 或 12.0.267.36
SEMVER34_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")

OUTPUT = os.environ.get("GITHUB_OUTPUT")

def parse_version(v: str) -> List[int]:
    return [int(x) for x in v.split(".")]

def pad_version(parts: List[int], length: int) -> List[int]:
    return parts + [0] * (length - len(parts))

def version_ge(a: str, b: str) -> bool:
    pa = parse_version(a)
    pb = parse_version(b)
    L = max(len(pa), len(pb))
    pa = pad_version(pa, L)
    pb = pad_version(pb, L)
    return pa >= pb

def load_list(path: str):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def main():
    print(f"[determine_versions] MIN_VERSION={MIN_VERSION} CAP={CAP}")

    os.makedirs("public", exist_ok=True)
    processed = load_list("public/version.json")
    failed = load_list("public/failed.json")
    processed_set = set(processed)
    failed_set = set(failed)

    # 获取 tags
    res = subprocess.run(["git", "ls-remote", "--tags", REPO_URL],
                         capture_output=True, text=True, check=True)
    tags = []
    for line in res.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):]
        tag = tag.split("^")[0]
        if SEMVER34_RE.match(tag):
            tags.append(tag)

    # 排序：按长度补齐再比较
    def sort_key(v):
        pv = parse_version(v)
        return pv + ([0] * (4 - len(pv)))  # 统一到长度 4 排序
    tags = sorted(set(tags), key=sort_key)

    unprocessed = [
        t for t in tags
        if version_ge(t, MIN_VERSION) and t not in processed_set and t not in failed_set
    ]

    batch = unprocessed[:CAP]
    leftover = max(0, len(unprocessed) - len(batch))

    include = []
    for v in batch:
        include.append({"os": "ubuntu-latest", "version": v})
        include.append({"os": "windows-latest", "version": v})

    versions_json = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
    matrix_json = json.dumps({"include": include}, ensure_ascii=False, separators=(",", ":"))
    has_versions = "true" if batch else "false"

    print(f"Total new(excluding processed & failed)={len(unprocessed)}, batch={len(batch)}, leftover={leftover}")
    print("Batch versions:", batch)
    print("Failed blacklist size:", len(failed_set))

    if OUTPUT:
        with open(OUTPUT, "a", encoding="utf-8") as out:
            out.write(f"versions={versions_json}\n")
            out.write(f"matrix={matrix_json}\n")
            out.write(f"has_versions={has_versions}\n")
            out.write(f"leftover_total={leftover}\n")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("determine_versions.py failed:", e, file=sys.stderr)
        sys.exit(1)
