#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Single-stage 3-way patch applier with:
  - In‑memory adaptive patch transformation (legacy Cast<T> -> T::cast) based on repository file probe.
  - Scoped conflict auto-resolution (OURS override strategy you requested earlier).
  - Token heuristic for success.
  - No semantic fallback.

Legacy adaptation trigger:
  If src/diagnostics/objects-printer.cc in the current checkout contains 'FixedArray::cast(*this)',
  we treat this version as using the older API style and rewrite ONLY added (+) lines from the patch:
     (v8::internal::)?Cast<Type>(expr)  ->  Type::cast(expr)
     v8::internal::Cast(                ->  v8::internal::Script::cast(
  The original patch.diff is NOT modified on disk; transformation is in-memory and piped to git apply.

Exit codes:
  0 = success (applied or token present, conflicts resolved)
  2 = failure (no token & apply failed, or unresolved conflicts)

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

# ---------- Subprocess helper ----------
def run(cmd: str, cwd: str, input_text: str = None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        shell=True,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

# ---------- Patch parsing ----------
def parse_changed_files(patch_text: str) -> List[str]:
    files = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path != "/dev/null":
                files.append(path)
    return files

# ---------- Legacy API detection & transformation ----------
CAST_TEMPLATE_RE = re.compile(r'\b(?:v8::internal::)?Cast<([A-Za-z_][A-Za-z0-9_:]*)>\s*\(')
CAST_PREFIX_RE   = re.compile(r'\bv8::internal::Cast\s*\(')

def needs_legacy_transform(root: str) -> bool:
    probe_path = os.path.join(root, "src/diagnostics/objects-printer.cc")
    if not os.path.isfile(probe_path):
        return False
    try:
        with open(probe_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return 'FixedArray::cast(*this)' in content
    except Exception:
        return False

def transform_added_line(line: str) -> str:
    # line starts with '+' (and not '+++ b/')
    body = line[1:]
    # Step 1: template Cast<T>
    def repl_template(m):
        t = m.group(1)
        return f'{t}::cast('
    body2 = CAST_TEMPLATE_RE.sub(repl_template, body)
    # Step 2: plain v8::internal::Cast(
    body3 = CAST_PREFIX_RE.sub('v8::internal::Script::cast(', body2)
    if body3 is not body:
        return '+' + body3
    return line

def maybe_transform_patch(root: str, patch_text: str, verbose: bool) -> str:
    if not needs_legacy_transform(root):
        if verbose:
            print("[transform] legacy pattern NOT detected -> no Cast<T> rewrite")
        return patch_text
    transformed_lines = []
    changed_count = 0
    for line in patch_text.splitlines(keepends=True):
        if line.startswith('+++ b/'):  # do not modify header lines
            transformed_lines.append(line)
            continue
        if line.startswith('+') and not line.startswith('+++ '):
            new_line = transform_added_line(line)
            if new_line != line:
                changed_count += 1
            transformed_lines.append(new_line)
        else:
            transformed_lines.append(line)
    if verbose:
        print(f"[transform] old-api detected -> rewritten + lines: {changed_count}")
    return ''.join(transformed_lines)

# ---------- Token check ----------
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

# ---------- Conflict detection ----------
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

# ---------- Conflict resolution (OURS override into THEIRS base) ----------
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

            result = list(theirs_clean)
            used = [False] * len(theirs_clean)

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
                    old_line = result[best_idx]
                    result[best_idx] = o_line
                    used[best_idx] = True
                    if verbose:
                        print(f"[conflict:{rel}] override theirs idx={best_idx} ratio={best_ratio:.2f}\n  OLD: {old_line!r}\n  NEW: {o_line!r}")
                else:
                    if verbose:
                        print(f"[conflict:{rel}] keep theirs (no match >= {threshold}) ours_line={o_line!r}")

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

# ---------- Main ----------
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
    proc = run("git reset --hard", cwd=root)
    if not os.path.isfile(patch_path):
        print(f"[ERROR] Patch file not found: {patch_path}", file=sys.stderr)
        return 2
    
    with open(patch_path, 'r', encoding='utf-8', errors='ignore') as f:
        original_patch_text = f.read()

    # In‑memory adaptive transform (only + lines) if legacy API detected
    patch_text = maybe_transform_patch(root, original_patch_text, args.verbose)

    changed_files = parse_changed_files(original_patch_text)  # use original list (paths unaffected)
    if args.verbose:
        print(f"[info] Changed files ({len(changed_files)}): {changed_files}")

    # 3-way apply via stdin
    proc = run("git apply --3way --whitespace=fix -", cwd=root, input_text=patch_text)
    if args.verbose:
        print("[git] return code:", proc.returncode)
        if proc.stdout.strip():
            print("[git] stdout:\n", proc.stdout)
        if proc.stderr.strip():
            print("[git] stderr:\n", proc.stderr)

    # Token heuristic
    token_found = file_contains_token(root, args.expect_file, args.expect_token,
                                      ci=args.case_insensitive_token)
    if args.verbose:
        print(f"[info] Token '{args.expect_token}' in {args.expect_file}: {token_found}")

    base_success = (proc.returncode == 0) or token_found

    # Conflict detection
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
