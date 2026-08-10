# Modern V8 CI failure analysis

This note separates compatibility failures from validation infrastructure
limits. The run history is intentionally retained; completed or cancelled runs
do not affect release selection.

## Initial 57-tag pass

The first pass used commit `e51a168` and grouped as many as five exact tags in
each Linux/Windows job. Four batches completed successfully and verified nine
tags. Most cancelled jobs reached the workflow's 330-minute timeout while
building several tags sequentially; one Linux half of batch 3 completed before
its Windows half timed out.

Three batches reached the real Electron cache fixture and reported a cache
magic mismatch on both tested platforms:

- V8 `14.8.178.38`: [run 31357606491](https://github.com/xqy2006/jsc2js/actions/runs/31357606491)
- V8 `15.0.1240245`: [run 31357633905](https://github.com/xqy2006/jsc2js/actions/runs/31357633905)
- V8 `15.2.124.5`: [run 31357648137](https://github.com/xqy2006/jsc2js/actions/runs/31357648137)

The first two runs completed with failure. In the third run, Linux recorded the
same fixture failure and the obsolete Windows half was later cancelled to free
a runner for the replacement validation.

## Corrections

Commit `eee4044` normalized the embedder-sized part of the cache magic in the
loader's private mutable copy. Follow-up single-version runs then exposed a
separate ordering problem: a deliberately invalid smoke-test file could reach
V8's payload view before its declared payload length was known to match the
file boundary.

Commit `c5ae94c` adds a fail-closed preflight before normalization. It requires:

1. at least the exact upstream `SerializedCodeData::kHeaderSize`;
2. a V8 code-cache magic family value;
3. an exact equality between `kPayloadLengthOffset` and the bytes remaining
   after the header; and
4. a total size representable by `AlignedCachedData`'s `int` length.

The loader still changes only its private cache copy. V8's normalized magic,
optional payload checksum, and deserializer protocol checks remain in place;
the deserializer sources are byte-identical to the selected V8 tag.

Commit `6bbd7c0` caps validation batches at three tags. This keeps the observed
per-job build time below the six-hour GitHub Actions limit while still using
parallel Linux and Windows runners. The final 57-tag pass is recorded in
[`modern-v8-final-ci.md`](modern-v8-final-ci.md).

The production release workflow applies the same bound: its six slots per OS
accept at most 18 versions per run, so no slot receives more than three
sequential V8 builds. Any remaining retry versions are carried into the next
automatically dispatched batch.

## Independent fixture-header check

Before the replacement builds completed, the repository's pinned generator
was also run locally with each exact Electron version. All three resulting
files satisfy the new preflight without requiring a V8 checkout:

| V8 | Electron | File bytes | Original magic | Declared payload | Bytes after 32-byte header |
|---|---|---:|---:|---:|---:|
| `14.8.178.38` | `42.8.1` | 1200 | `0xC0DE06C3` | 1168 | 1168 |
| `15.0.1240245` | `43.2.0` | 1200 | `0xC0DE06C6` | 1168 | 1168 |
| `15.2.124.5` | `44.0.0-beta.2` | 1192 | `0xC0DE06CF` | 1160 | 1160 |

The varying low magic bits are the embedder-specific external-reference table
size. The stable `0xC0DE` family bits and exact payload boundaries are the two
properties checked before the private-copy normalization.

## Replacement runtime evidence

Four single-version workflows at commit `6bbd7c0` completed successfully on
both Ubuntu 22.04 and Windows Server 2022:

| Purpose | V8 | Run |
|---|---|---|
| Modern lower boundary and `int` constant-pool length | `14.7.84` | [31380426377](https://github.com/xqy2006/jsc2js/actions/runs/31380426377) |
| `SafeHeapObjectSize` and real Electron 42.8.1 cache | `14.8.178.38` | [31380430852](https://github.com/xqy2006/jsc2js/actions/runs/31380430852) |
| Cast-traits predicate family and real Electron 43.2.0 cache | `15.0.1240245` | [31380434774](https://github.com/xqy2006/jsc2js/actions/runs/31380434774) |
| `DEF_CAST_TRAITS` family and real Electron 44.0.0-beta.2 cache | `15.2.124.5` | [31380442714](https://github.com/xqy2006/jsc2js/actions/runs/31380442714) |

On every platform, the short-header, non-V8-magic-family, and inconsistent
payload-length fixtures returned `Invalid JSC cache structure` without ending
the process. Each real Electron fixture printed five complete
`SharedFunctionInfo` and `BytecodeArray` sections and ended with
`JSC2JS_VALID_CACHE_OK`.

The final matrix's V8 15.2 batch was independently sampled from
[run 31380824056](https://github.com/xqy2006/jsc2js/actions/runs/31380824056).
Across V8 15.2.124, 15.2.124.1, and 15.2.124.5, all 18 invalid-fixture checks
(three structures, three versions, two platforms) were rejected as expected.
The exact Electron fixture for 15.2.124.5 printed five complete
`SharedFunctionInfo` and five `BytecodeArray` sections on each platform and
ended with `JSC2JS_VALID_CACHE_OK`.

The upper-bound V8 15.3.14/15.3.25 batch is recorded in
[run 31380830996](https://github.com/xqy2006/jsc2js/actions/runs/31380830996).
Its Windows job installed the required 10.0.28000.0 SDK family and completed
both tags. All 12 invalid-fixture checks (three structures, two versions, two
platforms) were rejected as expected. Every patch report changed exactly five
source files, retained the strict payload boundary preflight, and reported
`deserializer_modified: false`.

## Final exhaustive result

The replacement pass at commit `6bbd7c0` completed all 20 workflows and all 40
platform jobs successfully: 57/57 exact V8 tags were verified on both Ubuntu
22.04 and Windows Server 2022, with zero failed or active batches. Every run
used the same head SHA. The complete run mapping is preserved in
[`modern-v8-final-ci.md`](modern-v8-final-ci.md) and
[`modern-v8-final-ci.json`](modern-v8-final-ci.json).

The longest of the 40 platform jobs was the Windows V8 15.0.19 batch in
[run 31380753655](https://github.com/xqy2006/jsc2js/actions/runs/31380753655):
289.83 minutes. This leaves about 40 minutes before the workflow's 330-minute
job timeout and validates the three-version production slot limit against the
observed worst case.
