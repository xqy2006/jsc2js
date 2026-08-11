# jsc2js

[简体中文](#jsc2js) | [English](#english)

---

## 项目简介

`jsc2js` 用于将v8生成的 **JSC 字节码**逆向为可读的 JavaScript。  
本仓库主要包含两部分：

1. **修补后的 d8**：针对多个 V8 版本（见 Releases），为其内置一个用于加载与打印 `.jsc` 字节码的扩展（新增/修改的内建入口：`loadjsc()`）。
2. **集成的 View8 工具 (基于 [suleram/View8](https://github.com/suleram/View8) 并作定制修改)**：用于把 d8 打印出的字节码文本再还原/重建为 JavaScript 近似源码。


---

## 快速开始

### 1. 获取对应版本 d8

前往 Releases 页面，选择与你的目标 `.jsc` 生成环境 **相同的 V8 版本号**。 （如果没有找到，请发起 Issue ） 

每个版本下提供：
- `d8-linux`：Linux 64-bit 可执行文件
- `d8-windows.exe`：Windows 64-bit 可执行文件

### 2. 将 `.jsc` 转成字节码文本

```bash
./d8 -e "loadjsc('path/to/xxx.jsc')" > xxx.txt
```

说明：
- `loadjsc()` 为修补后 d8 注入的辅助函数。
- 输出的 `xxx.txt` 为人类可读（但仍较底层）的 V8 Ignition Bytecode 反汇编格式。

### 3. 使用修改后的 View8 转成 JavaScript

仓库结构中已包含定制的 `View8/` 目录：

```bash
cd View8
python view8.py --disassembled ../xxx.txt ../xxx.js
```

执行后：
- `xxx.js` 为基于字节码分析还原的近似 JavaScript。  
  （变量名 / 控制流结构可能与原始源码不同，属于“语义近似重建”）

### 4. 依赖环境

View8 需要：
- Python 3.9+（建议）
- 依赖
  ```bash
  pip install -r requirements.txt
  ```

---

## 说明

- 当前补丁覆盖 V8 5.1 及之后版本。V8 5.1–11.9 与 14.7.84+ 使用按源码
  API 自动适配的安全补丁；V8 12.0–14.7.83 继续使用内容未改动的稳定补丁。
- V8 5.1 是当前 `.jsc` 路径的兼容下界：截至 V8 5.0 的版本缺少本工具所需的
  `CodeSerializer::Deserialize` 路径。V8 5.9 才默认全面启用 Ignition，因此
  5.1–5.8 仅适用于宿主实际生成了 Ignition bytecode 的缓存。
- 旧版兼容层为兼容同一 V8 发布线的不同宿主（例如 upstream d8 与
  Electron），跳过 version、source、flags 三个宿主相关哈希；上游该版本已有
  的缓存检查均原样保留：369 个 tag 都有 magic 与 checksum，358 个有 header、
  356 个有 payload 长度、45 个有 CPU feature、20 个有只读快照 checksum 检查。
  反序列化器的同步与边界检查也完全不改。369 个精确 tag 已通过 Linux 和
  Windows 双平台构建验证，Issue #23 对应版本也包含在回归测试中。
- 对 V8 14.7.84–15.3.25 的 57 个精确失败 tag，现代兼容层识别
  `OwnedVector`、`DirectHandleVector`、对象谓词生成、`TrustedFixedArray` 及其
  强类型长度，并识别出四种组合的 API 边界。它只跳过 source、version、flags、
  宿主特定的只读快照身份校验，并在私有内存副本中规范化包含外部引用表大小的
  magic；规范化前会检查 V8 magic 家族以及 header 声明的 payload 长度与文件
  边界完全一致。上游 magic 检查仍执行，同时保留 payload checksum 以及全部
  反序列化协议检查；嵌套函数通过去重的平面
  GC 强根工作队列打印，不再递归展开 `HeapObjectShortPrint`。这 57 个精确 tag
  已通过补丁重放以及 Linux 和 Windows 双平台构建验证。
- 不同 V8 版本的 Bytecode 指令集、寄存槽布局、Handlers 表结构可能不同，请务必使用 **匹配版本** 的 d8。
- 由于没有node环境，由node编译出来的jsc可能无法正常反编译，electron则正常
- 如果输出异常，请：
  1. 再次核对版本；
  2. 若仍有异常，请提起 Issue
  3. 或自行修改 View8 的指令映射表。
- 还原 JS 不能 100% 重建原始源码：
  - 变量/函数名可能匿名或被重写；
  - 控制流可能被结构化重排；
  - 常量折叠或运行时优化不会完整还原。
 
---

## 参考与致谢

- View8：
  - [suleram/View8](https://github.com/suleram/View8) （已在本仓库中集成修改）
- 博客与资料参考：
  - https://guage.cool/wiz-license.html
  - https://rce.moe/2025/01/07/v8-bytecode-decompiler/
- V8 官方项目与文档 (Chromium / v8.dev)


---

# English

[简体中文](#jsc2js) | [English](#english)

---

## Overview

`jsc2js` reverses **V8‑generated JSC bytecode** into readable (approximate) JavaScript.

The repository contains two major parts:

1. **Patched d8**: Multiple V8 versions (see Releases) with an added builtin helper `loadjsc()` that loads and prints `.jsc` bytecode.
2. **Integrated View8 tool (based on a customized fork of [suleram/View8](https://github.com/suleram/View8))**: Converts the textual bytecode dump emitted by d8 into an approximate JavaScript reconstruction.

---

## Quick Start

### 1. Get the matching d8

Go to the Releases page and choose the V8 version that is **identical to** the one that produced your target `.jsc`. (Open an Issue if the version you need is missing.)

Each release provides:
- `d8-linux`: Linux 64‑bit executable
- `d8-windows.exe`: Windows 64‑bit executable

### 2. Convert `.jsc` into a bytecode text listing

```bash
./d8 -e "loadjsc('path/to/xxx.jsc')" > xxx.txt
```

Notes:
- `loadjsc()` is the injected helper in the patched d8.
- `xxx.txt` is a human‑readable (though still low‑level) V8 Ignition bytecode disassembly.

### 3. Use the modified View8 to reconstruct JavaScript

A customized `View8/` directory is included:

```bash
cd View8
python view8.py --disassembled ../xxx.txt ../xxx.js
```

Result:
- `xxx.js` contains an approximate JavaScript reconstruction based on the bytecode analysis.  
  (Identifiers / control flow may differ from the original source; this is a semantic approximation.)

### 4. Requirements

View8 requires:
- Python 3.9+ (recommended)
- Dependencies:
  ```bash
  pip install -r requirements.txt
  ```

---

## Notes

- The patch set covers V8 5.1 and later. V8 5.1–11.9 and 14.7.84+ use
  source-aware compatibility patchers; the stable V8 12.0–14.7.83 patch
  contents remain unchanged.
- V8 5.1 is the compatibility lower bound for this `.jsc` path. Releases
  through V8 5.0 lack the required `CodeSerializer::Deserialize` path.
  Ignition became universal by default in V8 5.9, so V8 5.1–5.8 applies only
  when the embedder actually emitted an Ignition bytecode cache.
- To support different embedders on the same V8 release line (for example,
  upstream d8 and Electron), the legacy compatibility layer bypasses the
  version, source, and flags hashes. Every cache check provided by that upstream
  V8 tag remains byte-for-byte in place: all 369 tags have magic and checksum
  checks, 358 have header checks, 356 payload-length checks, 45 CPU-feature
  checks, and 20 read-only-snapshot checksum checks. The deserializer's
  synchronization and bounds checks are unchanged. All 369 exact tags passed
  Linux and Windows builds, including regression coverage for issue #23.
- For the 57 exact failed tags from V8 14.7.84 through 15.3.25, the modern
  compatibility layer detects the `OwnedVector`, `DirectHandleVector`,
  generated object-predicate, `TrustedFixedArray`, and strong length API
  boundaries. It bypasses only the source, version, flags, and embedder-specific
  read-only-snapshot identity checks, and normalizes the external-reference-table
  size encoded in the private in-memory magic copy. Before normalization, it
  requires the V8 magic family and an exact match between the declared payload
  length and file boundary. The upstream magic check still executes; payload
  checksum and every deserializer protocol check are preserved. Nested
  functions are printed with a GC-rooted, deduplicated flat worklist instead
  of recursively expanding `HeapObjectShortPrint`. All 57 exact tags passed
  patch replay plus Linux and Windows builds.
- V8 bytecode opcodes, register/slot layouts, and handler table structures vary across versions. Always use a **matching** d8 build.
- Because there is no Node.js environment, the JSC compiled by Node.js may not be decompiled normally, while Electron works fine.
- If the output looks wrong:
  1. Re‑check the version alignment.
  2. If still incorrect, open an Issue.
  3. Or adjust the opcode mapping tables inside the modified View8.
- JavaScript reconstruction cannot be 100% identical to the original:
  - Variable / function names may be missing or replaced.
  - Control flow may be structurally reorganized.
  - Constant folding / runtime optimizations are not perfectly reversible.

---

## References & Acknowledgments

- View8:
  - [suleram/View8](https://github.com/suleram/View8) (integrated and modified here)
- Blog posts / materials:
  - https://guage.cool/wiz-license.html
  - https://rce.moe/2025/01/07/v8-bytecode-decompiler/
- Official V8 project & documentation (Chromium / v8.dev)
