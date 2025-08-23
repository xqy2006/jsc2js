#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改造点：
1. 先抓取 Node 官方 dist index，抽取所有 Node 发布记录中的 "v8" 字段，得到 Node 实际使用过的 V8 版本集合。
2. 再与 V8 仓库现有 tag 求交集（可通过 REQUIRE_TAG_MATCH 控制是否强制必须存在 tag）。
3. 仅对交集版本做 MIN_VERSION + 已处理/失败过滤 + CAP 截断，输出 versions。
4. 维护一个 public/node_v8_map.json：记录每个 V8 版本对应的 Node 版本列表，便于溯源。
"""

import json, os, re, subprocess, sys, urllib.request, urllib.error
from typing import List, Dict, Set

# 环境变量
MIN_VERSION = os.environ.get("MIN_VERSION", "12.0.1").strip()
REPO_URL = os.environ.get("V8_REPO", "https://chromium.googlesource.com/v8/v8.git")
NODE_INDEX_URL = os.environ.get("NODE_INDEX_URL", "https://nodejs.org/dist/index.json").strip()
REQUIRE_TAG_MATCH = os.environ.get("REQUIRE_TAG_MATCH", "true").lower() in ("1", "true", "yes", "on")

DEFAULT_CAP = 20
_raw_cap = os.environ.get("MAX_PER_RUN", "").strip()
try:
    CAP = int(_raw_cap) if _raw_cap else DEFAULT_CAP
    if CAP <= 0:
        CAP = DEFAULT_CAP
except ValueError:
    CAP = DEFAULT_CAP

# 支持 3 或 4 段语义版本
SEMVER34_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")
# Node dist index 里的 Node 版本形如 "v22.5.1"
NODE_VER_RE = re.compile(r"^v\d+\.\d+\.\d+$")

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

def load_map(path: str) -> Dict[str, Set[str]]:
    """
    读取已有的 node_v8_map.json，转换为 { v8_version: set(node_versions) }
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            m = {}
            for k, v in data.items():
                if isinstance(v, list):
                    m[k] = set(v)
            return m
    except Exception:
        pass
    return {}

def fetch_node_index(url: str):
    print(f"[determine_versions] Fetching Node index: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "v8-patch-workflow/1.0"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Fetch Node index failed: HTTP {resp.status}")
        data = resp.read()
    try:
        arr = json.loads(data.decode("utf-8"))
        if not isinstance(arr, list):
            raise ValueError("Node index JSON is not a list")
        return arr
    except Exception as e:
        raise RuntimeError(f"Parse Node index JSON failed: {e}") from e

def extract_v8_versions(node_index_list) -> (Dict[str, Set[str]], Set[str]):
    """
    返回:
      node_v8_map: { v8_version: set(node_versions_using_it) }
      v8_versions: set of v8_version
    """
    node_v8_map: Dict[str, Set[str]] = {}
    for entry in node_index_list:
        if not isinstance(entry, dict):
            continue
        node_ver = entry.get("version")
        v8_ver = entry.get("v8")
        if not node_ver or not isinstance(node_ver, str):
            continue
        if not NODE_VER_RE.match(node_ver):
            continue
        if not v8_ver or not isinstance(v8_ver, str):
            continue
        # 只接受 3 或 4 段
        if not SEMVER34_RE.match(v8_ver):
            continue
        node_v8_map.setdefault(v8_ver, set()).add(node_ver)
    v8_versions = set(node_v8_map.keys())
    return node_v8_map, v8_versions

def fetch_v8_tags(repo_url: str) -> Set[str]:
    print(f"[determine_versions] Fetching V8 tags: {repo_url}")
    res = subprocess.run(
        ["git", "ls-remote", "--tags", repo_url],
        capture_output=True, text=True, check=True
    )
    tags = set()
    for line in res.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):]
        # 去掉注释 tag^{}
        tag = tag.split("^")[0]
        if SEMVER34_RE.match(tag):
            tags.add(tag)
    print(f"[determine_versions] Total V8 tags matching semver(3/4) = {len(tags)}")
    return tags

def sort_key(v: str):
    pv = parse_version(v)
    return pv + ([0] * (4 - len(pv)))  # 补齐 4 段

def main():
    print(f"[determine_versions] MODE=Node-used-V8 MIN_VERSION={MIN_VERSION} CAP={CAP} REQUIRE_TAG_MATCH={REQUIRE_TAG_MATCH}")

    os.makedirs("public", exist_ok=True)
    processed = load_list("public/version.json")
    failed = load_list("public/failed.json")
    processed_set = set(processed)
    failed_set = set(failed)

    # 读取已有映射
    existing_map = load_map("public/node_v8_map.json")

    # 1. 抓取 Node index
    try:
        node_index = fetch_node_index(NODE_INDEX_URL)
    except Exception as e:
        print(f"[determine_versions] ERROR fetching Node index: {e}", file=sys.stderr)
        sys.exit(1)

    node_v8_map_new, node_used_v8_versions = extract_v8_versions(node_index)
    print(f"[determine_versions] Node index extracted V8 versions count = {len(node_used_v8_versions)}")

    # 合并老的映射（避免覆盖）
    for v8v, node_set in node_v8_map_new.items():
        if v8v not in existing_map:
            existing_map[v8v] = set()
        existing_map[v8v].update(node_set)

    # 2. 获取 V8 仓库 tags
    try:
        v8_tags = fetch_v8_tags(REPO_URL)
    except Exception as e:
        print(f"[determine_versions] ERROR fetching V8 tags: {e}", file=sys.stderr)
        sys.exit(1)

    if REQUIRE_TAG_MATCH:
        candidate_all = node_used_v8_versions & v8_tags
    else:
        # 不要求 tag 存在（极少场景），理论上还是建议 REQUIRE_TAG_MATCH=true
        candidate_all = node_used_v8_versions
    print(f"[determine_versions] Candidate (Node-used ∩ V8 tags) count = {len(candidate_all)}")

    # 3. 过滤 MIN_VERSION、过滤 processed/failed
    filtered = [
        v for v in candidate_all
        if version_ge(v, MIN_VERSION) and v not in processed_set and v not in failed_set
    ]

    filtered_sorted = sorted(filtered, key=sort_key)
    batch = filtered_sorted[:CAP]
    leftover = max(0, len(filtered_sorted) - len(batch))

    versions_json = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
    has_versions = "true" if batch else "false"

    print(f"[determine_versions] After filters: unprocessed_eligible={len(filtered_sorted)}, batch={len(batch)}, leftover={leftover}")
    print("Batch versions:", batch)
    print("Failed blacklist size:", len(failed_set))

    # 4. 写 node_v8_map.json（持久化 set -> list 排序）
    #    （只记录我们目前已在 node index 里看到的全部映射，不做 MIN_VERSION 限制）
    final_map_serializable = {
        v8v: sorted(list(node_versions), key=lambda s: [
            int(x) for x in s.lstrip("v").split(".")
        ])
        for v8v, node_versions in existing_map.items()
    }
    try:
        with open("public/node_v8_map.json", "w", encoding="utf-8") as f:
            json.dump(final_map_serializable, f, ensure_ascii=False, indent=2)
        print("[determine_versions] Updated public/node_v8_map.json")
    except Exception as e:
        print(f"[determine_versions] WARN: cannot write node_v8_map.json: {e}", file=sys.stderr)

    # 5. 输出到 GITHUB_OUTPUT
    if OUTPUT:
        with open(OUTPUT, "a", encoding="utf-8") as out:
            out.write(f"versions={versions_json}\n")
            out.write(f"has_versions={has_versions}\n")
            out.write(f"leftover_total={leftover}\n")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("determine_versions.py failed:", e, file=sys.stderr)
        sys.exit(1)
