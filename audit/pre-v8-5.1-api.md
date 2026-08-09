# Legacy V8 API audit

Exact tags audited: **57**

API families: **2**

Scope: exact V8 tags shipped by Node.js or Electron with 0.0.0 <= V8 < 5.1.0.

Compatibility result: all 57 tags lack the complete code-cache deserialization path required by jsc2js.

| Family | Range | Tags | Layout | Cache | Deserialize | Sanity | Objects |
|---|---:|---:|---|---|---|---|---|
| `334ea57214d8` | 2.0.5.4–4.9.385.28 | 52 | flat-d8 | unknown | `` | inline | raw-pointer |
| `f129a0e18ff5` | 5.0.71.35–5.0.71.52 | 5 | flat-d8 | unknown | `` | inline | raw-pointer |

The JSON report contains the source paths, exact API fingerprint, and compatibility result for every audited tag.
