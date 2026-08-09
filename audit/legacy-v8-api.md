# Legacy V8 API audit

Exact tags audited: **357**

API families: **14**

| Family | Range | Tags | Layout | Cache | Deserialize | Sanity | Objects |
|---|---:|---:|---|---|---|---|---|
| `5672a998aa2a` | 5.8.283.38–6.1.534.42 | 5 | flat-d8 | ScriptData | `MUST_USE_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isolate, S...` | inline | raw-pointer |
| `9efac280e320` | 6.2.414.32–6.6.346.32 | 6 | flat-d8 | ScriptData | `MUST_USE_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isolate, S...` | inline | raw-pointer |
| `230889991896` | 6.7.288.43–6.8.275.32 | 7 | flat-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | raw-pointer |
| `fb7860a80e0d` | 6.9.427.24–7.6.82 | 22 | flat-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | raw-pointer |
| `903e042d15ff` | 7.6.274–8.5.74 | 76 | split-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | value |
| `d86e7db27f60` | 8.5.189–8.9.163 | 28 | split-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | value |
| `5b08823a287a` | 8.9.231–9.1.127 | 15 | split-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | value |
| `f93630f2082c` | 9.1.269.19–9.4.66 | 31 | split-d8 | ScriptData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | inline | pointer-compression |
| `4cd31e3fff59` | 9.4.146.8–9.6.180.23 | 23 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split-inline-source | pointer-compression |
| `d41920ed580a` | 9.8.44–10.7.75.1 | 47 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split | pointer-compression |
| `11e3453d5f27` | 10.7.122–10.9.194.1 | 14 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split | pointer-compression |
| `2466f56351a3` | 11.0.133–11.5.150 | 45 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split | pointer-compression |
| `476fbe724f53` | 11.6.69–11.7.349 | 18 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split | tagged |
| `258c170a3508` | 11.8.29–11.9.169.4 | 20 | split-d8 | AlignedCachedData | `V8_WARN_UNUSED_RESULT static MaybeHandle<SharedFunctionInfo> Deserialize( Isolate* isol...` | split-readonly-checksum | tagged |

The JSON report contains the source paths, exact API fingerprint, and compatibility result for every audited tag.
