#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 public/update_needed.json 读取需要用 v2 补丁重新构建的版本列表，
按批次输出。不联网，不扫描新版本。
倒序：从最新版本开始处理。

环境变量：
  MAX_PER_RUN        (批次上限，默认为 20)
  GITHUB_OUTPUT      (GitHub Actions 传入，用于写输出)
"""
import json
import os
import sys
from typing import List

DEFAULT_CAP = 20
_raw_cap = os.environ.get("MAX_PER_RUN", "").strip()
try:
    CAP = int(_raw_cap) if _raw_cap else DEFAULT_CAP
    if CAP <= 0:
        CAP = DEFAULT_CAP
except ValueError:
    CAP = DEFAULT_CAP

OUTPUT = os.environ.get("GITHUB_OUTPUT")


def parse_version(v: str) -> List[int]:
    return [int(x) for x in v.split(".")]


def sort_versions_desc(versions: list) -> list:
    """按版本号从大到小排序（最新版本在前）"""
    def sort_key(v: str):
        parts = parse_version(v)
        return parts + [0] * (4 - len(parts))
    return sorted(set(versions), key=sort_key, reverse=True)


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
    print(f"[determine_update_versions] CAP={CAP}")

    os.makedirs("public", exist_ok=True)
    needed = load_list("public/update_needed.json")

    if not needed:
        print("[info] update_needed.json 为空或不存在，无需更新。")
        batch = []
        leftover = 0
    else:
        filtered = sort_versions_desc(needed)
        batch = filtered[:CAP]
        leftover = max(0, len(filtered) - len(batch))

    versions_json = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
    has_versions = "true" if batch else "false"

    print(f"待更新版本总数={len(needed)} 本批次={len(batch)} 剩余={leftover}")
    print("本批次版本列表:", batch)

    if OUTPUT:
        with open(OUTPUT, "a", encoding="utf-8") as out:
            out.write(f"versions={versions_json}\n")
            out.write(f"has_versions={has_versions}\n")
            out.write(f"leftover_total={leftover}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("determine_update_versions.py failed:", e, file=sys.stderr)
        sys.exit(1)
