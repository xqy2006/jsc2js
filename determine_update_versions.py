#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 public/update_needed.json 读取需要用 v2 补丁重新构建的版本列表，
按批次输出。支持分片模式用于多 workflow 并行。

环境变量：
  MAX_PER_RUN        (每个 workflow 处理的版本数上限，默认 6)
  SHARD_INDEX        (分片索引，从 0 开始，默认 0)
  SHARD_TOTAL        (总分片数，默认 1 = 不分片)
  GITHUB_OUTPUT      (GitHub Actions 传入，用于写输出)
"""
import json
import os
import sys
from typing import List

DEFAULT_CAP = 6
_raw_cap = os.environ.get("MAX_PER_RUN", "").strip()
try:
    CAP = int(_raw_cap) if _raw_cap else DEFAULT_CAP
    if CAP <= 0:
        CAP = DEFAULT_CAP
except ValueError:
    CAP = DEFAULT_CAP

SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))

OUTPUT = os.environ.get("GITHUB_OUTPUT")


def parse_version(v: str) -> List[int]:
    return [int(x) for x in v.split(".")]


def sort_versions(versions: list) -> list:
    def sort_key(v: str):
        parts = parse_version(v)
        return parts + [0] * (4 - len(parts))
    return sorted(set(versions), key=sort_key)


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
    print(f"[determine_update_versions] CAP={CAP} SHARD={SHARD_INDEX}/{SHARD_TOTAL}")

    os.makedirs("public", exist_ok=True)
    needed = load_list("public/update_needed.json")

    if not needed:
        print("[info] update_needed.json 为空或不存在，无需更新。")
        batch = []
        leftover = 0
    else:
        filtered = sort_versions(needed)

        # 分片：将整个列表均匀切给各个 shard
        if SHARD_TOTAL > 1:
            from math import ceil
            chunk_size = ceil(len(filtered) / SHARD_TOTAL)
            start = SHARD_INDEX * chunk_size
            end = min(len(filtered), start + chunk_size)
            shard_versions = filtered[start:end] if start < len(filtered) else []
            print(f"[shard] total={len(filtered)} chunk_size={chunk_size} shard[{SHARD_INDEX}]={len(shard_versions)} versions")
        else:
            shard_versions = filtered

        # 从本分片中取批次
        batch = shard_versions[:CAP]
        leftover = max(0, len(shard_versions) - len(batch))

    versions_json = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
    has_versions = "true" if batch else "false"

    print(f"待更新版本总数={len(needed)} 本分片={len(shard_versions) if needed else 0} 本批次={len(batch)} 剩余={leftover}")
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
