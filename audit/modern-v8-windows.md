# Modern V8 Windows SDK audit

Audited **57** exact modern V8 tags and **33** Chromium build revisions.

Result: **57 passed**, **0 failed**.

| V8 range | Tags | Exact Chromium SDK pin | Runner handling |
|---|---:|---|---|
| `14.7.84` – `15.3.14` | 56 | `10.0.26100.0` | native |
| `15.3.25` – `15.3.25` | 1 | `10.0.28000.0` | official Microsoft SDK installed on demand |

Both `setup_toolchain.py` and `vs_toolchain.py` must carry the same SDK pin for every exact build revision. Non-native SDK pins must be supported by the signed installer helper and invoked by compile, production, and rebuild workflows.

The 10.0.28000 installer is pinned from the [Microsoft Windows SDK download page](https://learn.microsoft.com/en-us/windows/apps/windows-sdk/downloads).

Only individual immutable V8 and Chromium build files were read; neither repository was cloned.
