# Legacy V8 API audit

Exact tags audited: **369**

API families: **17**

Scope: exact V8 tags shipped by Node.js or Electron with 5.1.0 <= V8 < 12.0.0.

V8 5.1 is the supported lower boundary; the separate pre-5.1 audit records why older tags are incompatible.

| Family | Range | Tags | Layout | Cache | Deserialize | Sanity | Objects | Predicate |
|---|---:|---:|---|---|---|---|---|---|
| `18c601cb14d4` | 5.1.281.47–5.2.361.43 | 4 | flat-d8 | ScriptData | `MUST_USE_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isolate, S...` | inline | raw-pointer | member |
| `375b52e0ae9b` | 5.3.332.37–5.3.332.47 | 3 | flat-d8 | ScriptData | `MUST_USE_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isolate, S...` | inline | raw-pointer | member |
| `eba56cd7fb2e` | 5.4.500.31–6.1.534.42 | 10 | flat-d8 | ScriptData | `MUST_USE_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isolate, S...` | inline | raw-pointer | member |
| `af5c1eb9df53` | 6.2.414.32–6.6.346.32 | 6 | flat-d8 | ScriptData | `MUST_USE_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isolate, S...` | inline | raw-pointer | member |
| `ade7cafb02ec` | 6.7.288.43–6.8.275.32 | 7 | flat-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | raw-pointer | member |
| `d417816a5654` | 6.9.427.24–7.2.502.25 | 9 | flat-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | raw-pointer | member |
| `75f626d9ec9b` | 7.3.492.10–7.6.82 | 13 | flat-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | value | member |
| `554fcdd7e5d9` | 7.6.274–8.5.74 | 76 | split-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | value | member |
| `c23089d71565` | 8.5.189–8.9.163 | 28 | split-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | value | member |
| `11bf19da142b` | 8.9.231–9.4.66 | 46 | split-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | value | member |
| `41f3fe21cb80` | 9.4.146.8–9.6.180.23 | 23 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split-inline-source | value | member |
| `d764240faeca` | 9.8.44–10.7.75.1 | 47 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split | value | member |
| `3e2d2ef53220` | 10.7.122–10.9.194.1 | 14 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split | value | member |
| `9bc099cc91e3` | 11.0.133–11.7.300 | 62 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split | value | member |
| `4a52f318d312` | 11.7.349–11.7.349 | 1 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split | value | free |
| `0028806bb864` | 11.8.29–11.8.29 | 1 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split-readonly-checksum | value | free |
| `ca3ce1f7c823` | 11.8.171–11.9.169.4 | 19 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split-readonly-checksum | tagged | free |

The JSON report contains the source paths, exact API fingerprint, and compatibility result for every audited tag.
