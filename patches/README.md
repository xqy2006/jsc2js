# V8 patches

Patch names describe the V8 range they target.  Build scripts must not select a
patch by an opaque generation suffix or by a single patch-level tag.

- `current/`: byte-stable V8 12.0–14.7.83 unified diffs used by normal builds.
- `legacy/apply_legacy_patch.py`: the source-aware V8 5.1–11.9 patcher.  It
  detects API/layout changes instead of applying fuzzy hunks across unrelated
  V8 versions.
- `modern/apply_modern_patch.py`: the source-aware V8 14.7.84+ patcher.  It
  supports the `OwnedVector`/`DirectHandleVector`, `TrustedFixedArray`, and
  strong length API families and fails closed when a future source layout is
  unknown.
- `archive/generation-1/` and `archive/generation-2/`: retained historical
  release patches.  They are not selected by the single-version validator.
- `archive/unsafe/v8-10.8-pr18.patch`: the original PR #18 patch, retained for
  audit only.  It disables deserializer protocol checks and recursively expands
  short object printing; do not use it for builds (see issue #23).

Current V8 ranges:

| File | V8 range |
|---|---|
| `current/v8-12.0-to-12.5.patch` | 12.0 through 12.5 |
| `current/v8-12.6-to-13.2.134.patch` | 12.6 through 13.2.134 |
| `current/v8-13.2.135-to-14.7.83.patch` | 13.2.135 through 14.7.83 |
| `modern/apply_modern_patch.py` | 14.7.84 and later (source-aware) |
