# Modern V8 Windows SDK audit

Audited **57** exact modern V8 tags and **33** Chromium build revisions.

Result: **57 passed**, **0 failed**.

| V8 range | Tags | Exact Chromium SDK pin | Runner handling |
|---|---:|---|---|
| `14.7.84` – `15.3.14` | 56 | `10.0.26100.0` | native |
| `15.3.25` – `15.3.25` | 1 | `10.0.28000.0` | checked alias to installed `10.0.26100.0` |

Both `setup_toolchain.py` and `vs_toolchain.py` must carry the same SDK pin for every exact build revision. Non-native SDK pins must be present in compile, production, and rebuild workflows.

Only individual immutable V8 and Chromium build files were read; neither repository was cloned.
