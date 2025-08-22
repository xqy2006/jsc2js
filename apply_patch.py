#!/usr/bin/env python3
"""
自定义模糊补丁应用工具（基于函数名定位 + 函数体内部模糊替换）

使用方式：
  在 v8/ 源码根目录下：
    python3 apply_patch.py --patch ../patch.diff
返回码：
  0 = 成功（全部需要的 hunk 已应用或已存在）
  2 = 失败（至少有一个 hunk 无法应用）

策略概要：
  1. 解析 unified diff -> 按文件拆分 -> 按 hunk 拆分。
  2. 对每个 hunk 判定是否“纯新增块”（没有任何 - 行且有 + 行）：
       - 若 hunk 新增代码中包含一个函数签名（正则匹配行尾 '{' 或 ')' 后跟 '{'），认为是“新增函数”。
       - 尝试根据同名函数是否已存在来决定是否插入。
  3. 对含有删除行的 hunk 视为“修改函数”：
       - 从 hunk 的 context / 删除 / 新增行中抽取函数签名候选
       - 定位源文件中函数起止行（基于大括号匹配）
       - 在函数体内按变更块做模糊替换（归一化后逐块查找）
  4. 若修改块无法定位但属于“仅新增行想插入”场景，则插入到函数体末尾（如果新增行不在函数体中）。
  5. 幂等：如果新增代码（首尾行）已存在，则跳过该 hunk。

可根据需要继续增强（如更复杂的编辑距离、跨函数拆分等）。
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

ANGLE_RE = re.compile(r"<[^>]*>")
SPACES_RE = re.compile(r"\s+")
LOCAL_NAME_RE = re.compile(r"Local<(?:Name|String)>")
COMMENT_PREFIX_RE = re.compile(r"^\s*//\s*")

# 判定函数签名的粗略正则（可扩展）
FUNC_SIG_RE = re.compile(
    r"""^\s*(?:static\s+|inline\s+|constexpr\s+|template<.*>\s*)*
        (?:[\w:<>~]+\s+)*       # 返回类型及模板等
        [A-Za-z_][\w:<>]*\s*    # 可能的类限定 + 名字
        ::?[\w~]+               # 函数名或类名::函数
        \s*\([^;]*\)\s*(?:const\s*)?(?:\{|$)""",
    re.VERBOSE,
)

@dataclass
class Hunk:
    header: str
    raw_lines: List[str] = field(default_factory=list)
    additions: List[str] = field(default_factory=list)
    deletions: List[str] = field(default_factory=list)
    context: List[str] = field(default_factory=list)

@dataclass
class FilePatch:
    path: str
    hunks: List[Hunk] = field(default_factory=list)

def normalize_line(line: str) -> str:
    """宽松归一化：去除注释前缀差异、空白、模板参数、Local<Name>/Local<String> 差异"""
    l = line.rstrip()
    # 去掉行首注释前缀（但保留内容用于识别已注释替换）
    if COMMENT_PREFIX_RE.match(l):
        l = COMMENT_PREFIX_RE.sub("", l, count=1)
    l = ANGLE_RE.sub("<T>", l)
    l = LOCAL_NAME_RE.sub("Local<T>", l)
    l = SPACES_RE.sub(" ", l).strip()
    return l

def is_function_signature(line: str) -> bool:
    return bool(FUNC_SIG_RE.match(line.strip()))

def parse_patch(patch_text: str) -> List[FilePatch]:
    files: List[FilePatch] = []
    current: Optional[FilePatch] = None
    current_hunk: Optional[Hunk] = None

    for line in patch_text.splitlines():
        if line.startswith("diff --git"):
            current = None
            current_hunk = None
        elif line.startswith("+++ b/"):
            path = line[6:].strip()
            current = FilePatch(path=path)
            files.append(current)
        elif line.startswith("@@ "):
            if current is None:
                continue
            current_hunk = Hunk(header=line.strip())
            current.hunks.append(current_hunk)
        else:
            if current_hunk is None:
                continue
            # 分类
            if line.startswith("+") and not line.startswith("+++"):
                current_hunk.raw_lines.append(line)
                current_hunk.additions.append(line[1:])
            elif line.startswith("-") and not line.startswith("---"):
                current_hunk.raw_lines.append(line)
                current_hunk.deletions.append(line[1:])
            else:
                # 可能是上下文行
                if line.startswith(" "):
                    current_hunk.context.append(line[1:])
                current_hunk.raw_lines.append(line)
    return files

def extract_candidate_function_names(hunk: Hunk) -> List[str]:
    candidates = []
    source_lines = hunk.context + hunk.deletions + hunk.additions
    for ln in source_lines:
        if is_function_signature(ln):
            # 提取函数名（粗略：抓取最后一个标识符( 之前）
            name_match = re.search(r"([A-Za-z_][\w:]*)\s*\(", ln)
            if name_match:
                candidates.append(name_match.group(1))
    # 去重保持顺序
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

def find_function_region(file_lines: List[str], func_name: str) -> Optional[Tuple[int, int]]:
    """
    在文件中找到函数定义的起止行索引（包含大括号）。
    基于 func_name 搜索函数签名，然后用括号/大括号计数。
    允许参数变化（如 Local<Name> vs Local<String>）。
    """
    norm_func_name = normalize_line(func_name)
    # 简单：逐行寻找包含 func_name 并能匹配 FUNC_SIG_RE
    for i, line in enumerate(file_lines):
        if func_name in line or norm_func_name in normalize_line(line):
            if is_function_signature(line):
                # 找 '{'
                brace_pos_line = line.find("{")
                start_idx = i
                depth = 0
                found_open = False
                # 可能 '{' 在后续行
                j = i
                while j < len(file_lines):
                    for ch in file_lines[j]:
                        if ch == "{":
                            depth += 1
                            found_open = True
                        elif ch == "}":
                            if found_open:
                                depth -= 1
                                if depth == 0:
                                    return (start_idx, j)
                    j += 1
                # 未正常闭合
    return None

def block_already_contains(all_lines: List[str], block_lines: List[str]) -> bool:
    """判断 block_lines (新增块) 是否已经整体出现过（宽松首尾匹配）"""
    meaningful = [l for l in block_lines if l.strip()]
    if not meaningful:
        return True
    first_norm = normalize_line(meaningful[0])
    last_norm = normalize_line(meaningful[-1])
    norm_file = [normalize_line(l) for l in all_lines]
    return (first_norm in norm_file) and (last_norm in norm_file)

def split_change_groups(hunk: Hunk):
    """
    将 hunk 划分为多个 (del_block, add_block) 组，保持顺序。
    典型 diff 结构：(- lines)(+ lines)(context)...
    """
    groups = []
    cur_del = []
    cur_add = []
    mode = None  # 'del' or 'add'
    for raw in hunk.raw_lines:
        if raw.startswith("-") and not raw.startswith("---"):
            if mode == 'add':
                if cur_del or cur_add:
                    groups.append((cur_del, cur_add))
                    cur_del, cur_add = [], []
            mode = 'del'
            cur_del.append(raw[1:])
        elif raw.startswith("+") and not raw.startswith("+++"):
            mode = 'add'
            cur_add.append(raw[1:])
        else:
            # context 行，结束当前组
            if cur_del or cur_add:
                groups.append((cur_del, cur_add))
                cur_del, cur_add = [], []
            mode = None
    if cur_del or cur_add:
        groups.append((cur_del, cur_add))
    return groups

def apply_change_groups_to_function(func_lines: List[str], groups) -> Tuple[List[str], bool]:
    """
    在函数体内应用变更组。
    返回 (新函数体行列表, 是否有修改)
    """
    changed = False
    for del_block, add_block in groups:
        # 如果是纯新增组（无删除）
        if not del_block and add_block:
            if block_already_contains(func_lines, add_block):
                continue
            # 追加到函数末尾（在最终的 '}' 之前）
            # 但 func_lines 不含最后 '}' 时要小心，这里假设传入时不去掉末尾
            # 先定位倒数第一个非空行的 '}' 位置
            insert_pos = len(func_lines) - 1
            # 向前找到最后一个仅含 '}' 的行
            for back in range(len(func_lines)-1, -1, -1):
                if func_lines[back].strip() == "}":
                    insert_pos = back
                    break
            func_lines = func_lines[:insert_pos] + add_block + func_lines[insert_pos:]
            changed = True
            continue

        # 有删除块的情况：尝试精确或宽松匹配
        norm_func = [normalize_line(l) for l in func_lines]
        norm_del = [normalize_line(l) for l in del_block if l.strip()]
        if not norm_del and add_block:
            # 退化：仍按追加
            if not block_already_contains(func_lines, add_block):
                insert_pos = len(func_lines) - 1
                for back in range(len(func_lines)-1, -1, -1):
                    if func_lines[back].strip() == "}":
                        insert_pos = back
                        break
                func_lines = func_lines[:insert_pos] + add_block + func_lines[insert_pos:]
                changed = True
            continue

        if not norm_del:
            continue

        # 寻找连续匹配段
        idx_found = -1
        for i in range(len(norm_func) - len(norm_del) + 1):
            segment = norm_func[i:i+len(norm_del)]
            if segment == norm_del:
                idx_found = i
                break
        if idx_found >= 0:
            # 替换
            before = func_lines[:idx_found]
            after = func_lines[idx_found + len(norm_del):]
            func_lines = before + add_block + after
            changed = True
        else:
            # 宽松：逐行匹配每个 del_line 在函数体任意位置，全部找到才进行替换(简化为注释/删除后再集中插入)
            positions = []
            for dnorm in norm_del:
                pos = next((i for i, ln in enumerate(norm_func) if ln == dnorm), None)
                if pos is None:
                    positions = []
                    break
                positions.append(pos)
            if positions:
                # 简化策略：按出现顺序删除，然后在第一个位置插入 add_block
                for p in sorted(set(positions), reverse=True):
                    del func_lines[p]
                ins = min(positions)
                func_lines = func_lines[:ins] + add_block + func_lines[ins:]
                changed = True
            else:
                # 删除块没有找到：尝试如果新增块已存在就视为已应用
                if block_already_contains(func_lines, add_block):
                    continue
                else:
                    # 放弃此组（视为失败：由上层决定 hunk 失败）
                    return func_lines, False
    return func_lines, changed

def apply_hunk(file_lines: List[str], hunk: Hunk, file_path: str) -> Tuple[List[str], bool]:
    """
    返回 (新的文件行, 是否成功应用该 hunk)
    """
    # 判断是否“新增函数”候选：无 deletions 且 additions 中出现新的函数签名行
    is_add_only = len(hunk.deletions) == 0 and len(hunk.additions) > 0
    add_func_sigs = [l for l in hunk.additions if is_function_signature(l)]

    if is_add_only and add_func_sigs:
        # 处理每个新增函数（通常一个）
        new_lines = file_lines[:]
        for sig in add_func_sigs:
            sig_norm = normalize_line(sig)
            # 已有则跳过
            if any(sig_norm == normalize_line(l) for l in new_lines):
                continue
            # 优先依据 hunk.context 的最后一行上下文来定位插入点
            anchor_line = None
            for c in reversed(hunk.context):
                c_norm = normalize_line(c)
                if any(c_norm == normalize_line(l) for l in new_lines):
                    anchor_line = c
                    break
            if anchor_line:
                # 在 anchor_line 所在行后插入整块新增（整个 hunk 的 additions，而不是单一函数签名行）
                idx = next(i for i,l in enumerate(new_lines) if normalize_line(l) == normalize_line(anchor_line))
                block = hunk.additions
                if not block_already_contains(new_lines, block):
                    new_lines = new_lines[:idx+1] + block + new_lines[idx+1:]
            else:
                # 附加到文件末尾前（保持换行）
                block = hunk.additions
                if not block_already_contains(new_lines, block):
                    if new_lines and new_lines[-1].strip():
                        new_lines.append("")  # 分隔空行
                    new_lines.extend(block)
        return new_lines, True

    # 修改函数路径
    # 先提取函数名候选
    func_candidates = extract_candidate_function_names(hunk)
    if not func_candidates:
        # 退化策略：如果只有新增没有 deletions，当作追加块
        if is_add_only:
            new_lines = file_lines[:]
            if not block_already_contains(new_lines, hunk.additions):
                new_lines.extend([""] + hunk.additions)
            return new_lines, True
        return file_lines, False

    new_file_lines = file_lines[:]
    for func_name in func_candidates:
        region = find_function_region(new_file_lines, func_name)
        if not region:
            continue
        start, end = region
        func_block = new_file_lines[start:end+1]
        body_changed = False

        groups = split_change_groups(hunk)
        # 将函数体应用 groups
        new_block, ok = apply_change_groups_to_function(func_block, groups)
        if ok:
            body_changed = True
            # 替换文件行
            new_file_lines = new_file_lines[:start] + new_block + new_file_lines[end+1:]
            return new_file_lines, True
        else:
            # 失败：尝试“仅新增”救援（如果所有组都是纯新增）
            only_add = all((not d and a) for d,a in groups)
            if only_add:
                # 在函数末尾插入所有新增行集合（去重）
                added = []
                for _, a in groups:
                    added.extend(a)
                if not block_already_contains(func_block, added):
                    insert_pos = len(func_block)-1
                    for back in range(len(func_block)-1, -1, -1):
                        if func_block[back].strip() == "}":
                            insert_pos = back
                            break
                    func_block = func_block[:insert_pos] + added + func_block[insert_pos:]
                    new_file_lines = new_file_lines[:start] + func_block + new_file_lines[end+1:]
                    return new_file_lines, True
            # 否则继续尝试下一个候选函数
            continue

    # 所有候选函数都失败
    return file_lines, False

def apply_file_patch(fp: FilePatch) -> bool:
    if not os.path.exists(fp.path):
        print(f"[WARN] {fp.path} 不存在，跳过。")
        return False
    with open(fp.path, "r", encoding="utf-8", errors="ignore") as f:
        original_lines = f.read().splitlines()

    current_lines = original_lines
    for idx, h in enumerate(fp.hunks, 1):
        new_lines, ok = apply_hunk(current_lines, h, fp.path)
        if not ok:
            print(f"[FAIL] 文件 {fp.path} 的第 {idx}/{len(fp.hunks)} 个 hunk 应用失败。")
            return False
        current_lines = new_lines

    if current_lines != original_lines:
        with open(fp.path, "w", encoding="utf-8") as f:
            f.write("\n".join(current_lines) + "\n")
        print(f"[APPLIED] {fp.path}")
    else:
        print(f"[NOCHANGE] {fp.path} (所有修改已存在)")
    return True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--patch", default="patch.diff", help="补丁路径")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.isfile(args.patch):
        print(f"补丁文件 {args.patch} 不存在", file=sys.stderr)
        return 2

    with open(args.patch, "r", encoding="utf-8", errors="ignore") as f:
        patch_text = f.read()

    file_patches = parse_patch(patch_text)
    if not file_patches:
        print("未解析到任何文件补丁内容。")
        return 2

    # 先统计期望文件数量，用于简单验证
    expected_paths = [fp.path for fp in file_patches]
    print("[INFO] 需要处理的文件：")
    for p in expected_paths:
        print("  -", p)

    all_ok = True
    for fp in file_patches:
        ok = apply_file_patch(fp)
        if not ok:
            all_ok = False
            break

    if args.dry_run:
        print("[DRY-RUN] 不落盘。")

    if all_ok:
        print("[RESULT] 补丁全部成功或已存在。")
        return 0
    else:
        print("[RESULT] 补丁存在失败 hunk。")
        return 2

if __name__ == "__main__":
    sys.exit(main())
