#!/usr/bin/env python3
"""
Semantic / fuzzy patch applier (function-name oriented).

Key features:
- Distinguish between "add new function/block" and "modify existing function".
- Locate functions by signature (loosely) and apply change groups.
- Fuzzy normalization: ignore whitespace, template args <...>, Local<Name>/Local<String> differences, leading //.
- Idempotent: if additions already present, skips.
- Pure ASCII log output to avoid Windows cp1252 encoding errors.
Exit codes:
  0 success (all hunks applied or already applied)
  2 failure (at least one hunk cannot be applied)
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ------------- Encoding safe print -------------
def safe_print(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        # Fallback: replace non-encodable characters
        encoded = text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace")
        print(encoded, **kwargs)

# ------------- Normalization regexes -------------
ANGLE_RE = re.compile(r"<[^>]*>")
SPACES_RE = re.compile(r"\s+")
LOCAL_NAME_RE = re.compile(r"Local<(?:Name|String)>")
COMMENT_PREFIX_RE = re.compile(r"^\s*//\s*")

# Rough function signature matcher
FUNC_SIG_RE = re.compile(
    r"""^\s*
        (?:static\s+|inline\s+|constexpr\s+|template<.*>\s*)*
        (?:[\w:<>~]+\s+)*       # return type and qualifiers
        [A-Za-z_][\w:<>]*       # possible class or return fragment
        (?:::)?[A-Za-z_][\w:<>]*  # name or Class::Name
        \s*\([^;{}]*\)\s*
        (?:const\s*)?
        (?:\{|$)
    """,
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

# ------------- Helpers -------------
def normalize_line(line: str) -> str:
    l = line.rstrip()
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

    for raw in patch_text.splitlines():
        if raw.startswith("diff --git"):
            current = None
            current_hunk = None
        elif raw.startswith("+++ b/"):
            path = raw[6:].strip()
            current = FilePatch(path=path)
            files.append(current)
        elif raw.startswith("@@ "):
            if current is None:
                continue
            current_hunk = Hunk(header=raw.strip())
            current.hunks.append(current_hunk)
        else:
            if current_hunk is None:
                continue
            if raw.startswith("+") and not raw.startswith("+++"):
                current_hunk.raw_lines.append(raw)
                current_hunk.additions.append(raw[1:])
            elif raw.startswith("-") and not raw.startswith("---"):
                current_hunk.raw_lines.append(raw)
                current_hunk.deletions.append(raw[1:])
            else:
                if raw.startswith(" "):
                    current_hunk.context.append(raw[1:])
                current_hunk.raw_lines.append(raw)
    return files

def extract_candidate_function_names(hunk: Hunk) -> List[str]:
    pool = hunk.context + hunk.deletions + hunk.additions
    cands = []
    for ln in pool:
        if is_function_signature(ln):
            m = re.search(r"([A-Za-z_][\w:]*)\s*\(", ln)
            if m:
                cands.append(m.group(1))
    seen = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

def find_function_region(file_lines: List[str], func_name: str) -> Optional[Tuple[int, int]]:
    # attempt to find start line with signature referencing func_name
    name_tokens = [func_name]
    norm_name = normalize_line(func_name)
    for i, line in enumerate(file_lines):
        norm_line = normalize_line(line)
        if (func_name in line or norm_name in norm_line) and is_function_signature(line):
            # Find opening brace (maybe on later lines)
            depth = 0
            found_open = False
            j = i
            # handle multi-line signature until first '{'
            while j < len(file_lines):
                for ch in file_lines[j]:
                    if ch == "{":
                        depth += 1
                        found_open = True
                    elif ch == "}":
                        if found_open:
                            depth -= 1
                            if depth == 0:
                                return (i, j)
                j += 1
    return None

def block_already_contains(all_lines: List[str], block_lines: List[str]) -> bool:
    filtered = [l for l in block_lines if l.strip()]
    if not filtered:
        return True
    first_norm = normalize_line(filtered[0])
    last_norm = normalize_line(filtered[-1])
    norms = [normalize_line(l) for l in all_lines]
    return first_norm in norms and last_norm in norms

def split_change_groups(hunk: Hunk):
    groups = []
    cur_del: List[str] = []
    cur_add: List[str] = []
    mode = None
    for raw in hunk.raw_lines:
        if raw.startswith("-") and not raw.startswith("---"):
            if mode == "add":
                groups.append((cur_del, cur_add))
                cur_del, cur_add = [], []
            mode = "del"
            cur_del.append(raw[1:])
        elif raw.startswith("+") and not raw.startswith("+++"):
            mode = "add"
            cur_add.append(raw[1:])
        else:
            if cur_del or cur_add:
                groups.append((cur_del, cur_add))
                cur_del, cur_add = [], []
            mode = None
    if cur_del or cur_add:
        groups.append((cur_del, cur_add))
    return groups

def apply_change_groups_to_function(func_lines: List[str], groups) -> Tuple[List[str], bool, bool]:
    """
    returns (new_func_lines, changed, fully_ok)
    fully_ok False means a fatal mismatch (a del block not found and cannot fallback)
    """
    changed = False
    for del_block, add_block in groups:
        # pure addition
        if not del_block and add_block:
            if block_already_contains(func_lines, add_block):
                continue
            insert_pos = len(func_lines) - 1
            for back in range(len(func_lines)-1, -1, -1):
                if func_lines[back].strip() == "}":
                    insert_pos = back
                    break
            func_lines = func_lines[:insert_pos] + add_block + func_lines[insert_pos:]
            changed = True
            continue

        norm_func = [normalize_line(l) for l in func_lines]
        del_norm = [normalize_line(l) for l in del_block if l.strip()]

        if not del_norm:
            # treat as addition only
            if add_block and not block_already_contains(func_lines, add_block):
                insert_pos = len(func_lines) - 1
                for back in range(len(func_lines)-1, -1, -1):
                    if func_lines[back].strip() == "}":
                        insert_pos = back
                        break
                func_lines = func_lines[:insert_pos] + add_block + func_lines[insert_pos:]
                changed = True
            continue

        # exact consecutive search
        idx_found = -1
        for i in range(len(norm_func) - len(del_norm) + 1):
            if norm_func[i:i+len(del_norm)] == del_norm:
                idx_found = i
                break

        if idx_found >= 0:
            before = func_lines[:idx_found]
            after = func_lines[idx_found + len(del_norm):]
            func_lines = before + add_block + after
            changed = True
            continue

        # fallback: line-by-line presence
        positions = []
        for dn in del_norm:
            pos = next((i for i, ln in enumerate(norm_func) if ln == dn), None)
            if pos is None:
                positions = []
                break
            positions.append(pos)

        if positions:
            for p in sorted(set(positions), reverse=True):
                del func_lines[p]
            ins = min(positions)
            func_lines = func_lines[:ins] + add_block + func_lines[ins:]
            changed = True
        else:
            # if add_block already there treat as ok
            if block_already_contains(func_lines, add_block):
                continue
            # cannot apply this group
            return func_lines, changed, False

    return func_lines, changed, True

def apply_hunk(file_lines: List[str], hunk: Hunk) -> Tuple[List[str], bool]:
    # Add-only new function?
    is_add_only = len(hunk.deletions) == 0 and len(hunk.additions) > 0
    add_func_sigs = [l for l in hunk.additions if is_function_signature(l)]

    if is_add_only and add_func_sigs:
        new_lines = file_lines[:]
        for sig in add_func_sigs:
            sig_norm = normalize_line(sig)
            if any(sig_norm == normalize_line(l) for l in new_lines):
                continue
            # try context anchor
            anchor = None
            for c in reversed(hunk.context):
                cn = normalize_line(c)
                if any(cn == normalize_line(l) for l in new_lines):
                    anchor = c
                    break
            block = hunk.additions
            if block_already_contains(new_lines, block):
                continue
            if anchor:
                idx = next(i for i, l in enumerate(new_lines) if normalize_line(l) == normalize_line(anchor))
                new_lines = new_lines[:idx+1] + block + new_lines[idx+1:]
            else:
                if new_lines and new_lines[-1].strip():
                    new_lines.append("")
                new_lines.extend(block)
        return new_lines, True

    # Modification path
    func_candidates = extract_candidate_function_names(hunk)
    if not func_candidates:
        # fallback: if purely additions treat as file-append
        if is_add_only:
            new_lines = file_lines[:]
            if not block_already_contains(new_lines, hunk.additions):
                new_lines.extend([""] + hunk.additions)
            return new_lines, True
        return file_lines, False

    new_file = file_lines[:]
    for fn in func_candidates:
        reg = find_function_region(new_file, fn)
        if not reg:
            continue
        s, e = reg
        block = new_file[s:e+1]
        groups = split_change_groups(hunk)
        new_block, changed, ok = apply_change_groups_to_function(block, groups)
        if ok:
            if changed:
                new_file = new_file[:s] + new_block + new_file[e+1:]
            return new_file, True
        else:
            # try fallback pure addition if all groups have only additions
            only_add = all((not d and a) for d, a in groups)
            if only_add:
                added_all = []
                for _, a in groups:
                    added_all.extend(a)
                if not block_already_contains(block, added_all):
                    insert_pos = len(block) - 1
                    for back in range(len(block)-1, -1, -1):
                        if block[back].strip() == "}":
                            insert_pos = back
                            break
                    block = block[:insert_pos] + added_all + block[insert_pos:]
                    new_file = new_file[:s] + block + new_file[e+1:]
                return new_file, True
            # else continue next candidate
            continue
    return file_lines, False

def apply_file_patch(fp: FilePatch, verbose: bool) -> bool:
    if not os.path.exists(fp.path):
        safe_print(f"[WARN] File {fp.path} not found.")
        return False
    with open(fp.path, "r", encoding="utf-8", errors="ignore") as f:
        orig = f.read().splitlines()
    curr = orig
    for idx, h in enumerate(fp.hunks, 1):
        new_lines, ok = apply_hunk(curr, h)
        if not ok:
            safe_print(f"[FAIL] {fp.path} hunk {idx}/{len(fp.hunks)} failed.")
            return False
        if new_lines != curr and verbose:
            safe_print(f"[INFO] {fp.path} hunk {idx} modified.")
        curr = new_lines
    if curr != orig:
        with open(fp.path, "w", encoding="utf-8") as f:
            f.write("\n".join(curr) + "\n")
        safe_print(f"[APPLIED] {fp.path}")
    else:
        safe_print(f"[NOCHANGE] {fp.path}")
    return True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--patch", default="patch.diff")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--report", default="apply_patch_report.txt")
    args = parser.parse_args()

    if not os.path.isfile(args.patch):
        safe_print(f"[ERROR] Patch file {args.patch} not found.")
        return 2

    with open(args.patch, "r", encoding="utf-8", errors="ignore") as f:
        patch_text = f.read()

    file_patches = parse_patch(patch_text)
    if not file_patches:
        safe_print("[ERROR] No file patches parsed.")
        return 2

    safe_print("[INFO] Files to process:")
    for fp in file_patches:
        safe_print(f"  - {fp.path}")

    all_ok = True
    modified_files = []
    failed_file = None

    for fp in file_patches:
        ok = apply_file_patch(fp, verbose=args.verbose)
        if not ok:
            all_ok = False
            failed_file = fp.path
            break
        else:
            # Quick check: if file changed, record
            # (We re-open to compare? Already flagged in apply_file_patch)
            pass

    status_line = "SUCCESS" if all_ok else f"FAIL ({failed_file})"
    if args.dry_run:
        safe_print("[DRY-RUN] No changes written.")
    safe_print(f"[RESULT] {status_line}")

    # Generate report
    try:
        with open(args.report, "w", encoding="utf-8") as r:
            r.write(f"result={status_line}\n")
            r.write(f"files={len(file_patches)}\n")
            if not all_ok and failed_file:
                r.write(f"failed_file={failed_file}\n")
    except Exception:
        pass

    return 0 if all_ok else 2

if __name__ == "__main__":
    sys.exit(main())
