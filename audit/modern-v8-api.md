# Modern V8 source API audit

Audited **57** exact V8 tags from 14.7.84 through 15.3.25 using raw GitHub source files only; the V8 repository was not cloned.

Result: **57 passed**, **0 failed**, across **4 detected API families**.

Every tag also exposes cache magic at byte offset 0, derives it from `ExternalReferenceTable::kSize`, publicly exposes the code cache header/payload boundary, and provides the little-endian read/write APIs used for private in-memory preflight and normalization.

| Family boundary | Tags | Object predicate generation | Reader / handle / rooted container | Constant pool / length |
|---|---:|---|---|---|
| `14.7.84` – `14.7.142` | 6 | `objects-h-macro` | `base::OwnedVector<char>` / `DirectHandle` / `DirectHandleVector` | `TrustedFixedArray` / `int` |
| `14.7.173` – `14.9.197` | 21 | `objects-h-macro` | `base::OwnedVector<char>` / `DirectHandle` / `DirectHandleVector` | `TrustedFixedArray` / `SafeHeapObjectSize` |
| `14.9.205` – `15.0.1240245` | 16 | `objects-inl-cast-traits` | `base::OwnedVector<char>` / `DirectHandle` / `DirectHandleVector` | `TrustedFixedArray` / `SafeHeapObjectSize` |
| `15.1.16` – `15.3.25` | 14 | `objects-inl-def-cast-traits` | `base::OwnedVector<char>` / `DirectHandle` / `DirectHandleVector` | `TrustedFixedArray` / `SafeHeapObjectSize` |

The semantic patch requires every API and safety anchor to match. An unknown future source shape fails before any source file is edited.
