#!/usr/bin/env python3
"""
Partition a list of versions into contiguous chunks for a given slot.
Reads:
  - VERSIONS_JSON (env): JSON array of versions, e.g. ["12.0.1","12.1.2.3"]
  - SLOT_INDEX (env): integer slot index (0 .. SLOTS_PER_OS-1)
  - SLOTS_PER_OS (env): total slots per OS (default 3)
Outputs (GITHUB_OUTPUT):
  - assigned_json: JSON array of versions assigned to this slot
  - has_any: 'true' / 'false'
"""
import json, os, sys

versions_raw = os.environ.get("VERSIONS_JSON", "[]")
slot_index = int(os.environ.get("SLOT_INDEX", "0"))
slots = int(os.environ.get("SLOTS_PER_OS", "3"))

try:
    versions = json.loads(versions_raw)
    assert isinstance(versions, list)
except Exception:
    print("Invalid VERSIONS_JSON", file=sys.stderr)
    versions = []

n = len(versions)
if n == 0 or slot_index >= slots:
    assigned = []
else:
    # contiguous chunk partition
    # chunk_size = ceil(n / slots)
    from math import ceil
    chunk_size = ceil(n / slots) if slots else n
    start = slot_index * chunk_size
    end = min(n, start + chunk_size)
    if start >= n:
        assigned = []
    else:
        assigned = versions[start:end]

assigned_json = json.dumps(assigned, separators=(",", ":"))
has_any = "true" if assigned else "false"

print(f"[partition] total={n} slots={slots} slot_index={slot_index} chunk_size={chunk_size if n else 0} assigned_count={len(assigned)}")
print(f"[partition] assigned={assigned}")

github_output = os.environ.get("GITHUB_OUTPUT")
if github_output:
    with open(github_output, "a", encoding="utf-8") as f:
        f.write(f"assigned_json={assigned_json}\n")
        f.write(f"has_any={has_any}\n")
else:
    print(f"assigned_json={assigned_json}")
    print(f"has_any={has_any}")
