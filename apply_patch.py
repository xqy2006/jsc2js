#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cross‑platform (Linux/Windows) 3‑way patch applier with optional legacy Cast<T> transformation.

Key improvements for Windows:
  - Binary-safe patch read (no decoding losses).
  - Optional output to a transformed patch file (no stdin EOL ambiguities).
  - Avoid implicit newline translation (preserve LF).
  - Allows forcing 'core.autocrlf=false' during apply to stabilize context matching.
  - Extra diagnostics: EOL style detection, git config dump.
  - Removable '--whitespace=fix' (now opt-in via --git-apply-extra).

Exit codes:
  0 = success (applied/token/conflicts resolved)
  2 = failure

Notable new arguments:
  --transformed-patch <file>   (default: patch_fix.diff)
  --no-write-transformed       (if set, still can use stdin apply)
  --apply-from-stdin           (force feeding patch via stdin, mainly for comparison)
  --force-no-autocrlf          (apply with -c core.autocrlf=false)
  --git-apply-extra "<args>"   (extra flags, e.g. "--whitespace=fix" if you really want it)
"""

import argparse
import difflib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Tuple

LABEL_OURS = "ours"
LABEL_THEIRS = "theirs"

RE_CONFLICT_START = re.compile(rf'^<<<<<<< {LABEL_OURS}\s*$')
RE_CONFLICT_MID   = re.compile(r'^=======\s*$')
RE_CONFLICT_END   = re.compile(rf'^>>>>>>> {LABEL_THEIRS}\s*$')

CAST_TEMPLATE_RE = re.compile(rb'\b(?:v8::internal::)?Cast<([A-Za-z_][A-Za-z0-9_:]*)>\s*\(')
CAST_PREFIX_RE   = re.compile(rb'\bv8::internal::Cast\s*\(')

def run(cmd, cwd=None, input_bytes=None, verbose=False):
    """
    cmd: list[str] preferred
    input_bytes: bytes or None
    """
    if verbose:
        print(f"[run] {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if verbose:
        if proc.stdout:
            print("[stdout]\n" + proc.stdout.decode(errors='replace'))
        if proc.stderr:
            print("[stderr]\n" + proc.stderr.decode(errors='replace'))
    return proc

def parse_changed_files(patch_bytes: bytes) -> List[str]:
    files = []
    for line in patch_bytes.splitlines():
        if line.startswith(b"+++ b/"):
            path = line[6:].decode(errors='replace').strip()
            if path != "/dev/null":
                files.append(path)
    return files

def needs_legacy_transform(root: str) -> bool:
    probe_path = os.path.join(root, "src/diagnostics/objects-printer.cc")
    if not os.path.isfile(probe_path):
        return False
    try:
        with open(probe_path, 'rb') as f:
            content = f.read()
        return b'FixedArray::cast(*this)' in content
    except Exception:
        return False

def transform_added_line(line: bytes) -> bytes:
    # line starts with b'+', not header
    body = line[1:]
    def repl_template(m):
        t = m.group(1)
        return t + b'::cast('
    body2 = CAST_TEMPLATE_RE.sub(repl_template, body)
    body3 = CAST_PREFIX_RE.sub(b'v8::internal::Script::cast(', body2)
    if body3 != body:
        return b'+' + body3
    return line

def maybe_transform_patch(root: str, patch_bytes: bytes, verbose=False) -> Tuple[bytes, int]:
    if not needs_legacy_transform(root):
        if verbose:
            print("[transform] legacy marker NOT detected -> skip Cast<T> rewrite")
        return patch_bytes, 0
    out_lines = []
    changed = 0
    for raw_line in patch_bytes.splitlines(keepends=True):
        if raw_line.startswith(b'+++ b/'):
            out_lines.append(raw_line)
            continue
        if raw_line.startswith(b'+') and not raw_line.startswith(b'+++ '):
            new_line = transform_added_line(raw_line.rstrip(b'\n\r'))
            # re-add original newline style (force LF for safety in patch)
            if raw_line.endswith(b'\r\n'):
                newline = b'\n'  # force unify to LF
            elif raw_line.endswith(b'\n'):
                newline = b'\n'
            else:
                newline = b'\n'
            if new_line != raw_line.rstrip(b'\n\r'):
                changed += 1
            out_lines.append(new_line + newline)
        else:
            # normalize patch lines to LF endings to avoid CR artifacts
            stripped = raw_line.rstrip(b'\r\n')
            out_lines.append(stripped + b'\n')
    if verbose:
        print(f"[transform] old-api detected -> rewritten + lines: {changed}")
    return b''.join(out_lines), changed

def file_contains_token(root: str, rel: str, token: str, ci: bool=False) -> bool:
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'rb') as f:
            data = f.read()
        if ci:
            return token.lower().encode() in data.lower()
        else:
            return token.encode() in data
    except Exception:
        return False

def detect_conflicts_in_files(root: str, files: List[str]) -> List[str]:
    marker = f"<<<<<<< {LABEL_OURS}".encode()
    conflict = []
    for rel in files:
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, 'rb') as f:
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
            result = list(theirs_clean)
            used = [False]*len(theirs_clean)
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
                out.append(line_text + '\n')
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
        with open(full, 'rb') as fd:
            if f"<<<<<<< {LABEL_OURS}".encode() not in fd.read():
                continue
        stat = resolve_conflicts_in_file(root, rel, threshold, verbose=verbose)
        stats.append(stat)
    return stats

def detect_eol_style(sample_bytes: bytes) -> str:
    if b'\r\n' in sample_bytes:
        return "CRLF"
    return "LF"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--patch', required=True, help='Original unified diff file')
    ap.add_argument('--root', default='.', help='Repository root')
    ap.add_argument('--report', default='apply_patch_report.txt')
    ap.add_argument('--expect-token', default='LoadJSC')
    ap.add_argument('--expect-file', default='src/d8/d8.h')
    ap.add_argument('--similarity-threshold', type=float, default=0.75)
    ap.add_argument('--no-auto-resolve', action='store_true')
    ap.add_argument('--case-insensitive-token', action='store_true')
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--transformed-patch', default='patch_fix.diff', help='Where to write transformed patch')
    ap.add_argument('--no-write-transformed', action='store_true', help='Do not write transformed patch file')
    ap.add_argument('--apply_from_stdin', action='store_true', help='Force apply via stdin (for comparison)')
    ap.add_argument('--force-no-autocrlf', action='store_true', help='Apply with -c core.autocrlf=false')
    ap.add_argument('--git_apply_extra', default='', help='Extra args for git apply, e.g. "--whitespace=fix"')
    ap.add_argument('--second-try-ignore-whitespace', action='store_true', help='If first apply fails, retry with --ignore-whitespace')
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    patch_path = os.path.abspath(args.patch)

    if not os.path.isfile(patch_path):
        print(f"[ERROR] Patch file not found: {patch_path}", file=sys.stderr)
        return 2

    # Hard reset
    run(["git", "reset", "--hard"], cwd=root, verbose=args.verbose)

    with open(patch_path, 'rb') as f:
        original_patch_bytes = f.read()

    original_eol = detect_eol_style(original_patch_bytes[:4000])
    if args.verbose:
        print(f"[diag] original patch EOL style detected: {original_eol}")

    transformed_patch_bytes, changed_count = maybe_transform_patch(root, original_patch_bytes, args.verbose)

    changed_files = parse_changed_files(original_patch_bytes)
    if args.verbose:
        print(f"[info] Changed files ({len(changed_files)}): {changed_files}")

    # Optionally write transformed patch to disk
    patch_to_apply_bytes = transformed_patch_bytes
    patch_file_to_apply = None
    if not args.no_write_transformed:
        patch_file_to_apply = os.path.join(root, args.transformed_patch)
        with open(patch_file_to_apply, 'wb') as f:
            f.write(patch_to_apply_bytes)
        if args.verbose:
            print(f"[info] Wrote transformed patch -> {patch_file_to_apply}")
    else:
        if args.verbose:
            print("[info] Not writing transformed patch (per flag)")

    # Build git apply command
    git_cmd = ["git"]
    if args.force_no_autocrlf:
        git_cmd += ["-c", "core.autocrlf=false"]
    git_cmd += ["apply", "--3way"]

    extra = args.git_apply_extra.strip()
    if extra:
        git_cmd += extra.split()

    # Strategy: apply from file unless user forces stdin or we suppressed writing file
    use_stdin = args.apply_from_stdin or (patch_file_to_apply is None)
    apply_proc = None

    if use_stdin:
        if args.verbose:
            print("[info] Applying patch via stdin")
        apply_proc = run(git_cmd + ["-"], cwd=root, input_bytes=patch_to_apply_bytes, verbose=args.verbose)
    else:
        if args.verbose:
            print(f"[info] Applying patch from file {patch_file_to_apply}")
        apply_proc = run(git_cmd + [patch_file_to_apply], cwd=root, verbose=args.verbose)

    first_rc = apply_proc.returncode

    # Optional second attempt with --ignore-whitespace if failed
    attempted_second = False
    if first_rc != 0 and args.second_try_ignore_whitespace:
        if args.verbose:
            print("[retry] First apply failed. Retrying with --ignore-whitespace after hard reset.")
        run(["git", "reset", "--hard"], cwd=root, verbose=args.verbose)
        second_cmd = list(git_cmd) + ["--ignore-whitespace"]
        if use_stdin:
            apply_proc = run(second_cmd + ["-"], cwd=root, input_bytes=patch_to_apply_bytes, verbose=args.verbose)
        else:
            apply_proc = run(second_cmd + [patch_file_to_apply], cwd=root, verbose=args.verbose)
        attempted_second = True

    final_rc = apply_proc.returncode

    token_found = file_contains_token(root, args.expect_file, args.expect_token,
                                      ci=args.case_insensitive_token)
    if args.verbose:
        print(f"[info] Token '{args.expect_token}' in {args.expect_file}: {token_found}")

    base_success = (final_rc == 0) or token_found

    conflict_files = detect_conflicts_in_files(root, changed_files)
    if args.verbose:
        print(f"[info] Conflict files: {conflict_files}")

    stats = []
    unresolved = False
    if conflict_files:
        if args.no_auto_resolve:
            unresolved = True
        else:
            stats = auto_resolve_conflicts(root, conflict_files, args.similarity_threshold, verbose=args.verbose)
            # Stage resolved ones
            resolved_files = [s.file for s in stats if not s.leftover]
            if resolved_files:
                run(["git", "add"] + resolved_files, cwd=root, verbose=args.verbose)
            still = detect_conflicts_in_files(root, conflict_files)
            if still:
                unresolved = True

    success = base_success and not unresolved

    # Diagnostics: show core.autocrlf
    git_cfg = run(["git", "config", "core.autocrlf"], cwd=root)
    core_autocrlf_val = git_cfg.stdout.decode().strip() if git_cfg.returncode == 0 else "(unset)"

    report_lines = [
        "Apply Patch Report",
        "==================",
        f"Original patch: {patch_path}",
        f"Transformed written: {not args.no_write_transformed} -> {patch_file_to_apply or '(stdin only)'}",
        f"Transformed + lines changed: {changed_count}",
        f"Patch original EOL style: {original_eol}",
        f"git core.autocrlf: {core_autocrlf_val}",
        f"First apply rc: {first_rc}",
    ]
    if attempted_second:
        report_lines.append(f"Second attempt rc: {final_rc}")
    report_lines += [
        f"Final rc used: {final_rc}",
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

    with open(os.path.join(root, args.report), 'w', encoding='utf-8') as r:
        r.write("\n".join(report_lines) + "\n")

    if args.verbose:
        print("\n".join(report_lines))

    return 0 if success else 2

if __name__ == '__main__':
    sys.exit(main())
