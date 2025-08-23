#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
根据 Node.js 与 Electron 的历史发布记录，提取它们曾使用过的 V8 版本，
只针对这些版本（且满足 MIN_VERSION、未处理、未失败）进行构建批处理。

环境变量：
  MIN_VERSION        (默认为 12.0.1 或 workflow 里传入)
  V8_REPO            (默认 https://github.com/v8/v8.git)
  MAX_PER_RUN        (批次上限，默认为 20)
  SOURCES            (逗号分隔: node, electron；默认 "node,electron")
  GITHUB_OUTPUT      (GitHub Actions 传入，用于写输出)
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from typing import List, Set, Iterable

MIN_VERSION = os.environ.get("MIN_VERSION", "12.0.1").strip()
REPO_URL = os.environ.get("V8_REPO", "https://github.com/v8/v8.git")
DEFAULT_CAP = 20
_raw_cap = os.environ.get("MAX_PER_RUN", "").strip()
try:
    CAP = int(_raw_cap) if _raw_cap else DEFAULT_CAP
    if CAP <= 0:
        CAP = DEFAULT_CAP
except ValueError:
    CAP = DEFAULT_CAP

SOURCES_RAW = os.environ.get("SOURCES", "").strip() or "node,electron"
SOURCES = {s.strip().lower() for s in SOURCES_RAW.split(",") if s.strip()}

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


def http_get_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            if resp.status != 200:
                print(f"[warn] GET {url} status={resp.status}", file=sys.stderr)
                return None
            data = resp.read()
            return json.loads(data.decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[warn] GET {url} failed: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[warn] GET {url} unknown error: {e}", file=sys.stderr)
        return None


def fetch_node_v8_versions() -> Set[str]:
    """
    Node.js 官方 dist index:
      https://nodejs.org/dist/index.json
    每个对象里通常有 "v8": "11.8.172.17"
    """
    url = "https://nodejs.org/dist/index.json"
    data = http_get_json(url)
    result = set()
    if isinstance(data, list):
        for itm in data:
            if not isinstance(itm, dict):
                continue
            v8v = itm.get("v8")
            if isinstance(v8v, str) and SEMVER34_RE.match(v8v):
                result.add(v8v)
    print(f"[info] Node releases parsed V8 versions: {len(result)}")
    return result


def fetch_electron_v8_versions() -> Set[str]:
    """
    Electron 可能的数据源：
      1) https://releases.electronjs.org/releases.json (官方聚合)
      2) https://raw.githubusercontent.com/electron/releases/master/lite.json
    结构示例(以 lite.json)：
      [
        {"version":"v28.1.0","deps":{"v8":"11.8.172.17","node":"18.18.2","chrome":"118.0.5993.159"}}, ...
      ]
    """
    urls = [
        "https://releases.electronjs.org/releases.json",
        "https://raw.githubusercontent.com/electron/releases/master/lite.json",
    ]
    result = set()
    for u in urls:
        data = http_get_json(u)
        if not data:
            continue
        if isinstance(data, list):
            for itm in data:
                if not isinstance(itm, dict):
                    continue
                # 可能在顶层或 itm['deps']['v8']
                v8v = None
                if "v8" in itm and isinstance(itm["v8"], str):
                    v8v = itm["v8"]
                else:
                    deps = itm.get("deps")
                    if isinstance(deps, dict):
                        v8v = deps.get("v8")
                if isinstance(v8v, str) and SEMVER34_RE.match(v8v):
                    result.add(v8v)
        # 如果第一个源已经拿到不少，后面仍继续合并（防止缺失）
    print(f"[info] Electron releases parsed V8 versions: {len(result)}")
    return result


def sort_versions(versions: Iterable[str]) -> List[str]:
    def sort_key(v: str):
        parts = parse_version(v)
        return parts + [0] * (4 - len(parts))
    return sorted(set(versions), key=sort_key)


def main():
    print(f"[determine_versions] MIN_VERSION={MIN_VERSION} CAP={CAP} SOURCES={','.join(sorted(SOURCES))}")

    os.makedirs("public", exist_ok=True)
    processed = load_list("public/version.json")
    failed = load_list("public/failed.json")
    processed_set = set(processed)
    failed_set = set(failed)

    # Step 1: 获取 Node / Electron 使用过的 V8 版本集合
    candidate_set: Set[str] = set()
    if "node" in SOURCES:
        candidate_set |= fetch_node_v8_versions()
    if "electron" in SOURCES:
        candidate_set |= fetch_electron_v8_versions()

    # 过滤非法格式
    candidate_set = {v for v in candidate_set if SEMVER34_RE.match(v)}
    if not candidate_set:
        print("[warn] 没有从指定来源获取到任何候选 V8 版本，直接退出。")
        batch = []
        leftover = 0
    else:
        # Step 2: 获取 v8 仓库的所有 tag，求交集
        try:
            res = subprocess.run(
                ["git", "ls-remote", "--tags", REPO_URL],
                capture_output=True,
                text=True,
                check=True
            )
            remote_tags = set()
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
                    remote_tags.add(tag)
        except subprocess.CalledProcessError as e:
            print(f"[error] 获取远端 tags 失败: {e}", file=sys.stderr)
            remote_tags = set()

        print(f"[info] Remote V8 tags count (semver 3/4): {len(remote_tags)}")

        existing_candidates = candidate_set & remote_tags
        missing = candidate_set - remote_tags
        if missing:
            print(f"[info] 跳过 {len(missing)} 个在 Node/Electron 中出现但远端无对应 tag 的版本(示例前 10): {list(sorted(missing))[:10]}")

        # Step 3: 按 MIN_VERSION / processed / failed 过滤
        filtered = [
            v for v in existing_candidates
            if version_ge(v, MIN_VERSION) and v not in processed_set and v not in failed_set
        ]
        filtered = sort_versions(filtered)

        # Step 4: 拆分批次
        batch = filtered[:CAP]
        leftover = max(0, len(filtered) - len(batch))

    include = []
    for v in batch:
        include.append({"os": "ubuntu-latest", "version": v})
        include.append({"os": "windows-latest", "version": v})

    versions_json = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
    matrix_json = json.dumps({"include": include}, ensure_ascii=False, separators=(",", ":"))
    has_versions = "true" if batch else "false"

    print(f"候选来源总数(candidate_set)={len(candidate_set)} 经过 tag 交集后={len(candidate_set & remote_tags) if candidate_set else 0}")
    print(f"最终可处理新版本(过滤 MIN_VERSION/processed/failed)={len(batch)} 剩余待后续处理={leftover}")
    print("本批次版本列表:", batch)
    print("失败黑名单大小:", len(failed_set))

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
