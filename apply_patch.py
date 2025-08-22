#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Single-stage 3-way patch applier with scoped conflict auto-resolution (OURS override mode).

Current strategy (as requested):
  - Run: git apply --3way --whitespace=fix <patch>
  - Ignore non-zero return code if an expected token (e.g. 'LoadJSC') is found in an expected file.
  - Conflict detection limited to files in the patch.
  - Conflict resolution (OURS override):
       THEIRS lines form the base.
       For each OURS line:
         * Find most similar unused THEIRS line (ratio >= threshold).
         * Replace that THEIRS line with the OURS line.
         * If no match >= threshold, keep THEIRS as-is (do NOT append OURS).
       Resulting block长度 == 原 THEIRS 长度（纯替换，不扩张）。
       (如果需要在无匹配时追加 OURS，可修改标注的注释。)
  - After resolution, stage resolved files (git add).
  - No semantic fallback.

Exit codes:
  0 success
  2 failure (unresolved conflicts OR token missing & git apply failed)

Arguments:
  --patch
  --root
  --report
  --expect-token
  --expect-file
  --similarity-threshold
  --no-auto-resolve
  --case-insensitive-token
  --verbose
"""

import argparse
import difflib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import List

LABEL_OURS = "ours"
LABEL_THEIRS = "theirs"

RE_CONFLICT_START = re.compile(rf'^<<<<<<< {LABEL_OURS}\s*$')
RE_CONFLICT_MID   = re.compile(r'^=======\s*$')
RE_CONFLICT_END   = re.compile(rf'^>>>>>>> {LABEL_THEIRS}\s*$')

def run(cmd: str, cwd: str):
    return subprocess.run(cmd, cwd=cwd, shell=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def parse_changed_files(patch_text: str) -> List[str]:
    files = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path != "/dev/null":
                files.append(path)
    return files

def file_contains_token(root: str, rel: str, token: str, ci: bool=False) -> bool:
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return token.lower() in content.lower() if ci else token in content
    except Exception:
        return False

def detect_conflicts_in_files(root: str, files: List[str]) -> List[str]:
    marker = f"<<<<<<< {LABEL_OURS}"
    conflict = []
    for rel in files:
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, 'r', encoding='utf-8', errors='ignore') as f:
                if marker in f.read():
                    conflict.append(rel)
        except Exception:
            pass
    return conflict

@dataclass
class ConflictStat:
    file: str
    blocks: int
    resolved: int
    leftover: bool

def resolve_conflicts_in_file(root: str, rel: str, threshold: float, verbose=False) -> ConflictStat:
    full = os.path.join(root, rel)
    with open(full, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    i = 0
    n = len(lines)
    out = []
    blocks = 0
    resolved = 0

    while i < n:
        if RE_CONFLICT_START.match(lines[i]):
            blocks += 1
            i += 1
            ours = []
            theirs = []
            while i < n and not RE_CONFLICT_MID.match(lines[i]):
                ours.append(lines[i]); i += 1
            if i >= n:
                out.extend(ours)
                break
            i += 1  # skip =======
            while i < n and not RE_CONFLICT_END.match(lines[i]):
                theirs.append(lines[i]); i += 1
            if i >= n:
                out.extend(ours + theirs)
                break
            i += 1  # skip >>>>>> theirs

            ours_clean = [l.rstrip('\n') for l in ours]
            theirs_clean = [l.rstrip('\n') for l in theirs]

            # OURS override:
            # Base: result = copy of theirs_clean
            result = list(theirs_clean)
            used = [False] * len(theirs_clean)  # track which THEIRS lines already replaced (to avoid double replace)

            for o_line in ours_clean:
                best_idx = -1
                best_ratio = 0.0
                for ti, t_line in enumerate(theirs_clean):
                    if used[ti]:
                        continue
                    ratio = difflib.SequenceMatcher(None, o_line, t_line).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_idx = ti
                if best_idx != -1 and best_ratio >= threshold:
                    # Replace that THEIRS line with OURS line
                    old_line = result[best_idx]
                    result[best_idx] = o_line
                    used[best_idx] = True
                    if verbose:
                        print(f"[conflict:{rel}] override theirs idx={best_idx} ratio={best_ratio:.2f}\n  OLD: {old_line!r}\n  NEW: {o_line!r}")
                else:
                    # 未匹配到足够相似的：保持原 THEIRS（不添加 ours）
                    if verbose:
                        print(f"[conflict:{rel}] keep theirs (no match >= {threshold}) ours_line={o_line!r}")
                    # 如果你想把未匹配的 ours 行追加，请取消下面注释：
                    # result.append(o_line)

            for line_text in result:
                if not line_text.endswith('\n'):
                    line_text += '\n'
                out.append(line_text)
            resolved += 1
        else:
            out.append(lines[i])
            i += 1

    with open(full, 'w', encoding='utf-8') as f:
        f.writelines(out)

    leftover = False
    with open(full, 'r', encoding='utf-8', errors='ignore') as f:
        if f"<<<<<<< {LABEL_OURS}" in f.read():
            leftover = True

    return ConflictStat(rel, blocks, resolved, leftover)

def auto_resolve_conflicts(root: str, files: List[str], threshold: float, verbose=False) -> List[ConflictStat]:
    stats = []
    for rel in files:
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        with open(full, 'r', encoding='utf-8', errors='ignore') as fd:
            if f"<<<<<<< {LABEL_OURS}" not in fd.read():
                continue
        stat = resolve_conflicts_in_file(root, rel, threshold, verbose=verbose)
        stats.append(stat)
    return stats

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--patch', required=True, help='Unified diff patch file')
    ap.add_argument('--root', default='.', help='Repository root')
    ap.add_argument('--report', default='apply_patch_report.txt')
    ap.add_argument('--expect-token', default='LoadJSC', help='Token indicating patch success (heuristic)')
    ap.add_argument('--expect-file', default='src/d8/d8.h', help='File to search token in')
    ap.add_argument('--similarity-threshold', type=float, default=0.75)
    ap.add_argument('--no-auto-resolve', dest='no_auto_resolve', action='store_true', help='Disable automatic conflict resolution')
    ap.add_argument('--case-insensitive-token', action='store_true', help='Case-insensitive token search')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    patch_path = os.path.abspath(args.patch)

    if not os.path.isfile(patch_path):
        print(f"[ERROR] Patch file not found: {patch_path}", file=sys.stderr)
        return 2

    with open(patch_path, 'r', encoding='utf-8', errors='ignore') as f:
        patch_text = f.read()

    changed_files = parse_changed_files(patch_text)
    if args.verbose:
        print(f"[info] Changed files ({len(changed_files)}): {changed_files}")

    # 3-way apply
    proc = run(f"git apply --3way --whitespace=fix {patch_path}", cwd=root)
    if args.verbose:
        print("[git] return code:", proc.returncode)
        if proc.stdout.strip():
            print("[git] stdout:\n", proc.stdout)
        if proc.stderr.strip():
            print("[git] stderr:\n", proc.stderr)

    token_found = file_contains_token(root, args.expect_file, args.expect_token,
                                      ci=args.case_insensitive_token)
    if args.verbose:
        print(f"[info] Token '{args.expect_token}' in {args.expect_file}: {token_found}")

    base_success = (proc.returncode == 0) or token_found

    conflict_files = detect_conflicts_in_files(root, changed_files)
    if args.verbose:
        print(f"[info] Conflict files: {conflict_files}")

    stats: List[ConflictStat] = []
    unresolved = False
    if conflict_files:
        if args.no_auto_resolve:
            unresolved = True
        else:
            stats = auto_resolve_conflicts(root, conflict_files, args.similarity_threshold, verbose=args.verbose)
            # stage resolved (no leftover)
            stage_candidates = [s.file for s in stats if not s.leftover]
            if stage_candidates:
                add_proc = run("git add " + " ".join(stage_candidates), cwd=root)
                if args.verbose:
                    print(f"[git] staging resolved files rc={add_proc.returncode}")
            still = detect_conflicts_in_files(root, conflict_files)
            if still:
                unresolved = True

    success = base_success and not unresolved

    report_lines = [
        "Apply Patch Report",
        "==================",
        f"3-way return code: {proc.returncode}",
        f"Token file: {args.expect_file}",
        f"Token searched: {args.expect_token} (case_insensitive={args.case_insensitive_token})",
        f"Token found: {token_found}",
        f"Base success (rc==0 or token): {base_success}",
    ]
    if conflict_files:
        report_lines.append(f"Initial conflict files: {conflict_files}")
    if stats:
        for st in stats:
            report_lines.append(
                f"Resolved {st.file}: blocks={st.blocks} resolved={st.resolved} leftover={st.leftover}"
            )
    report_lines.append(f"Unresolved conflicts: {unresolved}")
    report_lines.append(f"Final success: {success}")
    report_lines.append("Changed files:")
    for cf in changed_files:
        report_lines.append(f"  - {cf}")

    with open(args.report, 'w', encoding='utf-8') as r:
        r.write("\n".join(report_lines) + "\n")

    if args.verbose:
        print("\n".join(report_lines))

    return 0 if success else 2

if __name__ == '__main__':
    sys.exit(main())
