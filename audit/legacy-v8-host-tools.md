# Legacy V8 host-tool audit

Exact tags audited: **357**

Chromium build revisions: **167**

Windows toolchain templates: **5**

| Template | First V8 | Last V8 | Tags | Toolsets |
|---|---:|---:|---:|---|
| `args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64']` | 5.8.283.38 | 6.2.414.46 | 7 | v141, v142 |
| `args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64', '10.0.14393.0']` | 6.0.286.52 | 6.0.286.52 | 1 | v141 |
| `args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64', '10.0.15063.0']` | 6.6.346.24 | 6.6.346.32 | 3 | v141 |
| `args = [script_path, cpu_arg]` | 6.7.288.43 | 8.5.210.26 | 113 | v141, v142 |
| `args = [script_path, cpu_arg, ]` | 8.6.125 | 11.9.169.4 | 233 | current, v142 |

Every exact tag must match exactly one `vcvarsall` argument template and one environment-capture call, so CI can select both the historical MSVC headers and the SDK version actually installed on the runner. The build selects v142 for V8 5.x and 8.x–9.x, v141 for 6.x–7.x, and the current toolset for 10.x–11.x.
The JSON report records the exact V8 tag, Chromium build revision, template, and compatibility result.
