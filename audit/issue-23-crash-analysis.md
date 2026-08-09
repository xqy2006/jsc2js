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
  changes.
- Host-tool audit: 369 exact tags, 172 DEPS-selected Chromium build revisions,
  six Windows generator/toolchain templates, 231 historical-toolset tags with
  a forwarded legacy vcvars entry point, and an exact per-tag toolset selection
  (72 v141, 159 v142, 138 current). The Linux audit records three in-tree GYP
  tags, the single pre-sysroot-hook V8 5.2 tag, and 365 sysroot-hook tags. It
  also verifies the exact object-print argument name (3 GYP, 9 legacy GN, 357
  current GN); all templates are recognized before CI starts.
- V8 10.8.168.25 built successfully on Linux and Windows in
  [Actions run 31294049891](https://github.com/xqy2006/jsc2js/actions/runs/31294049891).
  Its malformed-cache smoke test exited normally with
  `JSC2JS_SAFE_REJECTION`.
- V8 11.8.29 built successfully on Linux and Windows in
  [Actions run 31294051640](https://github.com/xqy2006/jsc2js/actions/runs/31294051640).

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
