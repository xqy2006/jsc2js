#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 public/update_needed.json 读取需要用 v2 补丁重新构建的版本列表，
按批次输出。支持分片模式用于多 workflow 并行。

分片逻辑：先按 max_per_run 切成等大的块，再由 shard_index 选取对应的块。
这保证每个 shard 分到的版本数 <= max_per_run，不会有版本被跳过。

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
        total_remaining = 0
    else:
        filtered = sort_versions(needed)
        total_remaining = len(filtered)

        # 核心修复：以 CAP (max_per_run) 为块大小进行分片
        # 将整个列表切成每块 CAP 个版本，然后 shard_index 选取对应的块
        # 这样每个 shard 拿到的版本数 <= CAP，绝不会超出处理能力
        #
        # 示例：216 个版本，CAP=12，SHARD_TOTAL=15
        #   总共可切成 ceil(216/12) = 18 块
        #   shard 0 → 块 0 (版本 0-11)
        #   shard 1 → 块 1 (版本 12-23)
        #   ...
        #   shard 14 → 块 14 (版本 168-179)
        #   块 15、16、17 这次不处理（leftover）

        start = SHARD_INDEX * CAP
        end = min(total_remaining, start + CAP)

        if start >= total_remaining:
            batch = []
        else:
            batch = filtered[start:end]

        # leftover = 所有 shard 处理完后还剩多少版本
        total_handled = min(total_remaining, SHARD_TOTAL * CAP)
        leftover = max(0, total_remaining - total_handled)

    versions_json = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
    has_versions = "true" if batch else "false"

    print(f"待更新版本总数={total_remaining} 本分片[{SHARD_INDEX}]={len(batch)}个 "
          f"范围=[{SHARD_INDEX * CAP}:{SHARD_INDEX * CAP + len(batch)}) "
          f"全部shard处理后剩余={leftover}")
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
