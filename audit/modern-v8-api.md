# Modern V8 source API audit

Audited **57** exact V8 tags from 14.7.84 through 15.3.25 using raw GitHub source files only; the V8 repository was not cloned.

Result: **57 passed**, **0 failed**, across **3 detected API families**.

| Family boundary | Tags | Object predicate generation | Reader / handles | Constant pool |
|---|---:|---|---|---|
| `14.7.84` – `14.9.197` | 27 | `objects-h-macro` | `base::OwnedVector<char>` / `DirectHandle` | `TrustedFixedArray` |
| `14.9.205` – `15.0.1240245` | 16 | `objects-inl-cast-traits` | `base::OwnedVector<char>` / `DirectHandle` | `TrustedFixedArray` |
| `15.1.16` – `15.3.25` | 14 | `objects-inl-def-cast-traits` | `base::OwnedVector<char>` / `DirectHandle` | `TrustedFixedArray` |

The semantic patch requires every API and safety anchor to match. An unknown future source shape fails before any source file is edited.
