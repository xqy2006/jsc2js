# Issue #23 crash audit

## Scope and reproduction limits

Issue [#23](https://github.com/xqy2006/jsc2js/issues/23) reports that one of
two caches from Electron 22.3.27 / V8 10.8.168.25 aborts on both Linux and
Windows. The report contains stack traces but not the failing `.jsc`, so the
exact input cannot be reproduced from the issue alone. The alternating stack
frames and the libc++ `vector[] index out of bounds` assertion are consistent
with recursive object printing or a deserializer stream that has lost
synchronization; this is an evidence-based diagnosis, not a claim about the
missing input's exact call stack.

The V8 audit downloaded only individual source files and DEPS-selected build
templates. It did not clone the V8 repository.

## Stable V8 12+ baseline

The three existing V8 12+ patch contents were not changed. Their Git blob IDs
before and after the rename are identical:

| Current name | Original name | Git blob |
|---|---|---|
| `patches/current/v8-12.0-to-12.5.patch` | `patch_old_v3.diff` | `baa4e8f1cc4e5465fbe797a641b3e7e8c1ae1246` |
| `patches/current/v8-12.6-to-13.2.134.patch` | `patch_v3.diff` | `cddc24c96135bafd720bcb428023eea0dfb93262` |
| `patches/current/v8-13.2.135-plus.patch` | `patch_1_v3.diff` | `3ec516d1cbf30e54e2f25879db21a7d4a429fca5` |

All three V8 12+ patches validate an object before expanded short printing by
recovering its isolate, checking the map pointer, and checking the map against
the read-only `meta_map`. They also bound their explicit bytecode traversal by
both a visited-address set and a depth limit. PR [#18](https://github.com/xqy2006/jsc2js/pull/18)
ported the traversal to V8 10.8 but omitted the isolate/map/meta-map guard.

The V8 12+ implementation remains the repository's empirically stable
baseline and is intentionally untouched. The legacy implementation does not
assume that the same internal-object behavior is safe on older V8 branches.

Several PR #18 changes were plainly adapted from that stable baseline, so
their mere presence is not enough to explain the older-branch crash. The
high-signal differential is the missing object-validity guard:

| Behavior | Stable V8 12+ | PR #18 (10.8) | New V8 5.1–11.9 path |
|---|---|---|---|
| Full cache sanity bypass | Yes | Yes | No |
| Relaxed deserializer checks | Yes | Yes | No |
| Recursive short-print expansion | Yes | Yes | No |
| isolate/map/meta-map guard before short print | Yes | **No** | Short printer unchanged |
| Explicit traversal cycle bound | visited set + depth | visited set + depth | flat visited work list |

This comparison also rules out the explicit traversal's missing depth limit as
the explanation: PR #18 already had both a visited set and a depth limit.
Because V8 serializer layouts and heap APIs changed substantially before V8
12, the older implementation avoids the unnecessary permissive changes rather
than assuming their V8 12+ runtime behavior carries backward unchanged.

## Unsafe PR #18 behavior

The original PR #18 patch is retained only as
`patches/archive/unsafe/v8-10.8-pr18.patch`. It is never selected by the build
script. Its relevant changes include:

- returning success without checking cache magic, V8 version, flags, payload
  length, or checksum;
- weakening deserializer synchronization, size, end-position, and unreachable
  checks, sometimes substituting `undefined` after a protocol error;
- changing read-only allocations and disabling `Rehash()`;
- recursively expanding `FixedArray` and `SharedFunctionInfo` from
  `HeapObjectShortPrint` without the V8 12+ map-validity guard.

Continuing after a cache-format or stream mismatch can create a partially
decoded object graph. Recursively printing that graph is the highest-risk
combination found in the audit and matches the repeated-frame shape reported
in issue #23.

## Crash-safe legacy design

The V8 5.1–11.9 patcher follows a narrower design:

| Area | Legacy behavior |
|---|---|
| Embedder-dependent hashes | Version, source, and flags hashes bypassed |
| Upstream cache checks | Preserved exactly; magic/checksum on all 369 tags, header on 358, length on 356, CPU feature on 45, read-only snapshot checksum on 20 |
| Deserializer | Unchanged |
| `HeapObjectShortPrint` | Unchanged |
| Object type predicates | Selected from the exact `Object` declaration: 349 member-verifier tags and 20 static/free-function tags |
| Nested functions | Flat work list of `Handle<SharedFunctionInfo>` |
| Cycle handling | Handle identity checked before every print |
| Invalid cache | JavaScript exception; process must remain alive |

The default V8 short printer therefore remains brief and non-recursive. Nested
functions are discovered only from bytecode constant pools, and each SFI is
printed at most once.

## Validation coverage

- Source/API audit: 369 exact Node/Electron V8 tags, 16 API families,
  369 compatible and zero fetch failures.
- Semantic patch replay: 369/369 exact tags; exactly four V8 source files
  change for each tag across 14 patch templates, and no deserializer file
  changes. The predicate form is audited independently from the
  `FixedArray::get` return type: [V8 11.7.349's `objects.h`](https://github.com/v8/v8/blob/11.7.349/src/objects/objects.h)
  still declares `EXPORT_DECL_VERIFIER(Object)`, while
  [V8 11.8.29's `objects.h`](https://github.com/v8/v8/blob/11.8.29/src/objects/objects.h)
  switches to `EXPORT_DECL_STATIC_VERIFIER(Object)` and requires the free
  `IsSharedFunctionInfo(object)` form. All 20 audited V8 11.8–11.9 tags use
  that static/free-function API.
- Host-tool audit: 369 exact tags, 172 DEPS-selected Chromium build revisions,
  161 DEPS-selected Chromium tools/clang revisions, six Windows
  generator/toolchain templates, 231 historical-toolset tags with a forwarded
  legacy vcvars entry point, and an exact per-tag toolset selection (87 v141,
  144 v142, 138 current). V8 8.0–8.1's pinned clang 10 therefore uses v141;
  V8 8.2's clang 11 is the first 8.x family to use v142. The selected Visual
  Studio year is checked against
  every exact `vs_toolchain.py`; for the 93 tags whose clang hook indexes a
  keyed DIA DLL table, it is checked against that exact table as well. The
  Linux audit records three in-tree GYP tags, the single pre-sysroot-hook V8
  5.2 tag (hosted clang and lld), and 365 sysroot-hook tags. It also verifies
  the exact object-print argument name (3 GYP, 9 legacy GN, 357 current GN);
  all templates are recognized before CI starts. For all 366 external-GN tags,
  the setup-toolchain transform is replayed twice to prove idempotence and the
  complete result is tokenized, including both single-line and multi-line
  ATL/MFC assertion forms. The installed SDK argument is also required to stay
  ahead of the injected `-vcvars_ver` switch, preserving the exact upstream
  vcvars argument order. The replay also verifies that the original UM-library
  and linker output statements remain present after the multi-line assertion
  edit; this catches a syntactically valid but over-broad edit that would delete
  unrelated setup code. There are 215 exact templates with a `vc_lib_um_path`
  fallback anchor, of which the 208 historical-toolset tags actively receive a
  checked `Lib/<version>/um/<arch>` fallback when vcvars does not report a UM
  directory and `User32.Lib` exists there. The exact `vs_toolchain.py` transform
  is also replayed for all 228 external-GN historical-toolset tags. In 133 tags,
  beginning at V8 8.1.197, the Chromium helper no longer reads
  `GYP_MSVS_VERSION`; those templates receive an insertion-only bridge so their
  build helper sees logical VS2017/2019 while CI runs on VS 2022. In 25 of those
  tags (V8 8.1.197 through 8.4.191), the exact pinned clang hook consumes that
  logical year for a keyed DIA DLL lookup. Every replay is tokenized,
  idempotent, and required to preserve all original lines in order.
- The exact hosted-VS/DIA regression range, V8 8.3.110.5 through 8.3.110.12,
  completed all five tags on Linux and Windows in
  [Actions run 31311301043](https://github.com/xqy2006/jsc2js/actions/runs/31311301043).
  The Windows job crossed the previously failing pinned clang DIA hook and
  completed the patched builds and smoke checks.
- The complete V8 8.7.220.25 through 8.8.186 batch, including the checkout
  that originally exceeded Windows' legacy path limit at V8 8.8.74, completed
  all five tags on both platforms in
  [Actions run 31317674121](https://github.com/xqy2006/jsc2js/actions/runs/31317674121).
- The depot_tools bootstrap regression range completed all five V8 9.4 tags
  on both platforms in
  [Actions run 31321149072](https://github.com/xqy2006/jsc2js/actions/runs/31321149072).
  depot_tools is allowed to bootstrap once, then `DEPOT_TOOLS_UPDATE=0` pins
  it for the remaining fetch, hook, patch, and build steps; this avoids both
  an uninitialized checkout and a late self-update network failure.
- V8 10.8.168.25 built successfully on Linux and Windows in
  [Actions run 31294049891](https://github.com/xqy2006/jsc2js/actions/runs/31294049891).
  Its malformed-cache smoke test exited normally with
  `JSC2JS_SAFE_REJECTION`.
- V8 11.8.29 built successfully on Linux and Windows in
  [Actions run 31294051640](https://github.com/xqy2006/jsc2js/actions/runs/31294051640).
- V8 11.8.171 built successfully on Linux and Windows in
  [Actions run 31296954463](https://github.com/xqy2006/jsc2js/actions/runs/31296954463).

The first CI pass with a source-hash-only bypass generated a real cache with
Electron 22.3.27 and safely rejected it on both platforms instead of crashing
([run 31295635048](https://github.com/xqy2006/jsc2js/actions/runs/31295635048)):
Electron reports `10.8.168.25-electron.0`, whose version and flags hashes differ
from upstream V8 10.8.168.25.

The final test bypasses only those three embedder-dependent hashes. In
[run 31297502040](https://github.com/xqy2006/jsc2js/actions/runs/31297502040),
both Linux and Windows generated a 1,024-byte cache with Electron 22.3.27,
parsed it with the patched upstream V8 10.8.168.25 d8, printed five complete
`SharedFunctionInfo` sections, exited normally, and ended with
`JSC2JS_VALID_CACHE_OK`. This synthetic cache exercises the matching runtime
path but does not replace reproduction with the unavailable issue attachment.
