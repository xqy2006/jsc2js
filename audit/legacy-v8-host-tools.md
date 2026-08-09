# Legacy V8 host-tool audit

Exact tags audited: **369**

Chromium build revisions: **172**

Windows toolchain templates: **6**

CI legacy `VC/vcvarsall.bat` bridge tags: **231**

Linux host modes: `hosted-clang-in-tree-gyp` **3**, `hosted-clang-without-sysroot-hook` **1**, `pinned-clang-with-sysroot-hook` **365**

| Template | First V8 | Last V8 | Tags | Toolsets |
|---|---:|---:|---:|---|
| `in-tree GYP/Ninja with imported vcvarsall environment` | 5.1.281.47 | 5.1.281.65 | 3 | v142 |
| `args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64']` | 5.2.361.43 | 6.2.414.46 | 16 | v141, v142 |
| `args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64', '10.0.14393.0']` | 6.0.286.52 | 6.0.286.52 | 1 | v141 |
| `args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64', '10.0.15063.0']` | 6.6.346.24 | 6.6.346.32 | 3 | v141 |
| `args = [script_path, cpu_arg]` | 6.7.288.43 | 8.5.210.26 | 113 | v141, v142 |
| `args = [script_path, cpu_arg, ]` | 8.6.125 | 11.9.169.4 | 233 | current, v142 |

V8 5.1 is audited against its in-tree GYP/Ninja generator and imports the selected hosted `vcvarsall` environment directly. V8 5.2 predates the Linux sysroot hook, so CI disables the missing Wheezy sysroot and routes the pinned clang paths to the hosted compiler. Every later exact tag must match one `vcvarsall` argument template and one environment-capture call, so CI can select both the historical MSVC headers and the SDK version actually installed on the runner. The build selects v142 for V8 5.x and 8.x–9.x, v141 for 6.x–7.x, and the current toolset for 10.x–11.x. For every tag where a historical toolset is selected and the pinned setup script retains the legacy path, CI provides a forwarding `VC/vcvarsall.bat` entry point.
The JSON report records the exact V8 tag, Chromium build revision, template, and compatibility result.
