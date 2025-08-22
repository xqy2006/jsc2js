#!/usr/bin/env python3
import argparse, os, re, sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

def safe_print(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"), **kwargs)

ANGLE_RE = re.compile(r"<[^>]*>")
SPACES_RE = re.compile(r"\s+")
LOCAL_NAME_RE = re.compile(r"Local<(?:Name|String)>")
COMMENT_PREFIX_RE = re.compile(r"^\s*//\s*")
FUNC_SIG_RE = re.compile(
    r"""^\s*(?:static\s+|inline\s+|constexpr\s+|template<.*>\s*)*
        (?:[\w:<>~]+\s+)*[A-Za-z_][\w:<>]*::?[A-Za-z_][\w:<>]*\s*\([^;{}]*\)\s*(?:const\s*)?(?:\{|$)""",
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
    l = line.rstrip()
    if COMMENT_PREFIX_RE.match(l):
        l = COMMENT_PREFIX_RE.sub("", l, count=1)
    l = ANGLE_RE.sub("<T>", l)
    l = LOCAL_NAME_RE.sub("Local<T>", l)
    l = SPACES_RE.sub(" ", l).strip()
    return l

def is_function_signature(line: str) -> bool:
    return bool(FUNC_SIG_RE.match(line.strip()))

def parse_patch(text: str) -> List[FilePatch]:
    files, current, current_hunk = [], None, None
    for raw in text.splitlines():
        if raw.startswith("diff --git"):
            current = None
            current_hunk = None
        elif raw.startswith("+++ b/"):
            p = raw[6:].strip()
            current = FilePatch(path=p)
            files.append(current)
        elif raw.startswith("@@ "):
            if current is None: continue
            current_hunk = Hunk(header=raw.strip())
            current.hunks.append(current_hunk)
        else:
            if current_hunk is None: continue
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

def extract_candidate_function_names(h: Hunk) -> List[str]:
    cands, pool = [], h.context + h.deletions + h.additions
    for ln in pool:
        if is_function_signature(ln):
            m = re.search(r"([A-Za-z_][\w:]*)\s*\(", ln)
            if m:
                cands.append(m.group(1))
    out, seen = [], set()
    for c in cands:
        if c not in seen:
            seen.add(c); out.append(c)
    return out

def find_function_region(lines: List[str], name: str) -> Optional[Tuple[int,int]]:
    nname = normalize_line(name)
    for i, line in enumerate(lines):
        if name in line or nname in normalize_line(line):
            if is_function_signature(line):
                depth=0; opened=False; j=i
                while j < len(lines):
                    for ch in lines[j]:
                        if ch == "{": depth +=1; opened=True
                        elif ch == "}":
                            if opened:
                                depth -=1
                                if depth==0: return (i,j)
                    j+=1
    return None

def block_already_contains(all_lines: List[str], block: List[str]) -> bool:
    filt = [l for l in block if l.strip()]
    if not filt: return True
    fn = normalize_line(filt[0]); ln = normalize_line(filt[-1])
    norms = [normalize_line(l) for l in all_lines]
    return fn in norms and ln in norms

def split_groups(h: Hunk):
    groups=[]; d=[]; a=[]; mode=None
    for raw in h.raw_lines:
        if raw.startswith("-") and not raw.startswith("---"):
            if mode=="add": groups.append((d,a)); d=[]; a=[]
            mode="del"; d.append(raw[1:])
        elif raw.startswith("+") and not raw.startswith("+++"):
            mode="add"; a.append(raw[1:])
        else:
            if d or a: groups.append((d,a)); d=[]; a=[]
            mode=None
    if d or a: groups.append((d,a))
    return groups

def apply_groups_in_func(func_lines: List[str], groups) -> Tuple[List[str], str, bool]:
    changed=False
    for (del_block, add_block) in groups:
        if not del_block and add_block:
            if block_already_contains(func_lines, add_block): continue
            insert_pos = len(func_lines)-1
            for i in range(len(func_lines)-1, -1, -1):
                if func_lines[i].strip()=="}": insert_pos=i; break
            func_lines = func_lines[:insert_pos]+add_block+func_lines[insert_pos:]
            changed=True
            continue
        del_norm=[normalize_line(l) for l in del_block if l.strip()]
        norm_func=[normalize_line(l) for l in func_lines]
        if not del_norm:
            if add_block and not block_already_contains(func_lines, add_block):
                insert_pos=len(func_lines)-1
                for i in range(len(func_lines)-1,-1,-1):
                    if func_lines[i].strip()=="}": insert_pos=i; break
                func_lines=func_lines[:insert_pos]+add_block+func_lines[insert_pos:]
                changed=True
            continue
        # contiguous
        idx=-1
        for i in range(len(norm_func)-len(del_norm)+1):
            if norm_func[i:i+len(del_norm)]==del_norm:
                idx=i; break
        if idx>=0:
            before=func_lines[:idx]; after=func_lines[idx+len(del_norm):]
            func_lines=before+add_block+after
            changed=True
            continue
        # scattered
        positions=[]
        for dn in del_norm:
            p=next((k for k,x in enumerate(norm_func) if x==dn), None)
            if p is None: positions=[]; break
            positions.append(p)
        if positions:
            for p in sorted(set(positions), reverse=True):
                del func_lines[p]
            ins=min(positions)
            func_lines=func_lines[:ins]+add_block+func_lines[ins:]
            changed=True
            continue
        if block_already_contains(func_lines, add_block):
            continue
        return func_lines, "FAILED_GROUP", False
    return func_lines, ("MODIFIED" if changed else "NOCHANGE"), True

def file_level_apply(lines: List[str], h: Hunk) -> Tuple[List[str], str]:
    groups=split_groups(h)
    cur=lines[:]
    norm=[normalize_line(l) for l in cur]
    changed=False
    for del_block, add_block in groups:
        if not del_block and add_block:
            if block_already_contains(cur, add_block): continue
            # anchor
            anchor=None
            for c in reversed(h.context):
                cn=normalize_line(c)
                if cn in norm: anchor=cn; break
            if anchor:
                ai=next(i for i,x in enumerate(norm) if x==anchor)
                cur=cur[:ai+1]+add_block+cur[ai+1:]
            else:
                if cur and cur[-1].strip(): cur.append("")
                cur.extend(add_block)
            norm=[normalize_line(l) for l in cur]
            changed=True
            continue
        # deletion present
        del_norm=[normalize_line(l) for l in del_block if l.strip()]
        if not del_norm:
            if add_block and not block_already_contains(cur, add_block):
                if cur and cur[-1].strip(): cur.append("")
                cur.extend(add_block); norm=[normalize_line(l) for l in cur]; changed=True
            continue
        # contiguous
        idx=-1
        for i in range(len(norm)-len(del_norm)+1):
            if norm[i:i+len(del_norm)]==del_norm: idx=i; break
        if idx>=0:
            cur=cur[:idx]+add_block+cur[idx+len(del_norm):]
            norm=[normalize_line(l) for l in cur]; changed=True; continue
        # scattered ordered
        pos=[]; start=0
        for dn in del_norm:
            p=next((j for j in range(start,len(norm)) if norm[j]==dn), None)
            if p is None: pos=[]; break
            pos.append(p); start=p+1
        if pos:
            first,last=pos[0],pos[-1]
            cur=cur[:first]+add_block+cur[last+1:]
            norm=[normalize_line(l) for l in cur]; changed=True; continue
        if add_block and block_already_contains(cur, add_block):
            continue
        return lines, "FAILED"
    return cur, ("MODIFIED" if changed else "NOCHANGE")

def apply_hunk(file_lines: List[str], h: Hunk, file_path: str, hidx: int, total: int) -> Tuple[List[str], bool, str]:
    is_add_only = len(h.deletions)==0 and len(h.additions)>0
    add_func_sigs=[l for l in h.additions if is_function_signature(l)]
    if is_add_only and add_func_sigs:
        new=file_lines[:]; changed=False
        for sig in add_func_sigs:
            sn=normalize_line(sig)
            if any(sn==normalize_line(x) for x in new): continue
            anchor=None
            for c in reversed(h.context):
                if normalize_line(c) in [normalize_line(x) for x in new]:
                    anchor=c; break
            block=h.additions
            if block_already_contains(new, block): continue
            if anchor:
                ai=next(i for i,x in enumerate(new) if normalize_line(x)==normalize_line(anchor))
                new=new[:ai+1]+block+new[ai+1:]
            else:
                if new and new[-1].strip(): new.append("")
                new.extend(block)
            changed=True
        status="MODIFIED" if changed else "NOCHANGE"
        return new, True, status

    func_candidates=extract_candidate_function_names(h)
    if not func_candidates:
        new,status=file_level_apply(file_lines, h)
        ok = status!="FAILED"
        return new, ok, status if ok else "FAILED"

    new_file=file_lines[:]
    for fn in func_candidates:
        region=find_function_region(new_file, fn)
        if not region: continue
        s,e=region
        block=new_file[s:e+1]
        groups=split_groups(h)
        new_block, status, ok=apply_groups_in_func(block, groups)
        if ok:
            if status=="MODIFIED":
                new_file=new_file[:s]+new_block+new_file[e+1:]
            return new_file, True, status
        else:
            # fallback pure add
            only_add=all((not d and a) for d,a in groups)
            if only_add:
                merged=[]
                for _,a in groups: merged.extend(a)
                if not block_already_contains(block, merged):
                    ip=len(block)-1
                    for i in range(len(block)-1,-1,-1):
                        if block[i].strip()=="}": ip=i; break
                    block=block[:ip]+merged+block[ip:]
                    new_file=new_file[:s]+block+new_file[e+1:]
                return new_file, True, "MODIFIED"
            # try next candidate
            continue
    # final fallback
    new,status=file_level_apply(file_lines, h)
    ok = status!="FAILED"
    return new, ok, status if ok else "FAILED"

def apply_file_patch(fp: FilePatch, verbose: bool) -> bool:
    if not os.path.exists(fp.path):
        safe_print(f"[WARN] Missing {fp.path}")
        return False
    with open(fp.path,"r",encoding="utf-8",errors="ignore") as f:
        original=f.read().splitlines()
    current=original
    total=len(fp.hunks)
    for i,h in enumerate(fp.hunks,1):
        new_lines, ok, status=apply_hunk(current,h,fp.path,i,total)
        if not ok:
            safe_print(f"[HUNK] file={fp.path} index={i} status=FAILED")
            return False
        if verbose or status!="NOCHANGE":
            safe_print(f"[HUNK] file={fp.path} index={i} status={status}")
        current=new_lines
    if current!=original:
        with open(fp.path,"w",encoding="utf-8") as f:
            f.write("\n".join(current)+"\n")
        safe_print(f"[APPLIED] {fp.path}")
    else:
        safe_print(f"[NOCHANGE] {fp.path}")
    return True

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--patch",default="patch.diff")
    p.add_argument("--verbose",action="store_true")
    p.add_argument("--report",default="apply_patch_report.txt")
    args=p.parse_args()

    if not os.path.isfile(args.patch):
        safe_print("[ERROR] Patch file not found.")
        return 2
    with open(args.patch,"r",encoding="utf-8",errors="ignore") as f:
        patch_text=f.read()
    fps=parse_patch(patch_text)
    if not fps:
        safe_print("[ERROR] No file patches parsed.")
        return 2
    safe_print("[INFO] Files to process:")
    for fp in fps:
        safe_print(f"  - {fp.path}")

    all_ok=True; failed=None
    for fp in fps:
        if not apply_file_patch(fp, args.verbose):
            all_ok=False; failed=fp.path; break
    result="SUCCESS" if all_ok else f"FAIL ({failed})"
    safe_print(f"[RESULT] {result}")
    try:
        with open(args.report,"w",encoding="utf-8") as r:
            r.write(f"result={result}\n")
            if failed: r.write(f"failed_file={failed}\n")
    except Exception: pass
    return 0 if all_ok else 2

if __name__=="__main__":
    sys.exit(main())
