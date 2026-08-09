# Legacy V8 host-tool audit

Exact tags audited: **357**

Chromium build revisions: **167**

Windows toolchain templates: **5**

| Template | First V8 | Last V8 | Tags | v142 tags |
|---|---:|---:|---:|---:|
| `args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64']` | 5.8.283.38 | 6.2.414.46 | 7 | 7 |
| `args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64', '10.0.14393.0']` | 6.0.286.52 | 6.0.286.52 | 1 | 1 |
| `args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64', '10.0.15063.0']` | 6.6.346.24 | 6.6.346.32 | 3 | 3 |
| `args = [script_path, cpu_arg]` | 6.7.288.43 | 8.5.210.26 | 113 | 113 |
| `args = [script_path, cpu_arg, ]` | 8.6.125 | 11.9.169.4 | 233 | 95 |

For V8 before 10.0, every exact tag must match exactly one `vcvarsall` argument template so the installed MSVC v142 toolset can be selected.
The JSON report records the exact V8 tag, Chromium build revision, template, and compatibility result.
