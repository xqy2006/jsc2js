# Legacy V8 API audit

Exact tags audited: **57**

API families: **3**

Scope: exact V8 tags shipped by Node.js or Electron with 0.0.0 <= V8 < 5.1.0.

Compatibility result: all 57 tags lack the complete code-cache deserialization path required by jsc2js.

| Family | Range | Tags | Layout | Cache | Deserialize | Sanity | Objects | Predicate |
|---|---:|---:|---|---|---|---|---|---|
| `cfd40f9b4d45` | 2.0.5.4–3.22.24.19 | 34 | flat-d8 | unknown | `` | inline | raw-pointer | unknown |
| `79dcd75056e3` | 3.28.71.19–4.5.103.37 | 12 | flat-d8 | unknown | `` | inline | raw-pointer | member |
| `a6cf73ff5078` | 4.6.85.28–5.0.71.52 | 11 | flat-d8 | unknown | `` | inline | raw-pointer | member |

The JSON report contains the source paths, exact API fingerprint, and compatibility result for every audited tag.
