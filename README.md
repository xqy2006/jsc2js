# jsc2js

简体中文 | [English](#english)

---

## 项目简介

`jsc2js` 用于将v8生成的 **JSC 字节码**逆向为可读的 JavaScript。  
本仓库主要包含两部分：

1. **修补后的 d8**：针对多个 V8 版本（见 Releases），为其内置一个用于加载与打印 `.jsc` 字节码的扩展（新增/修改的内建入口：`loadjsc()`）。
2. **集成的 View8 工具 (基于 [suleram/View8](https://github.com/suleram/View8) 并作定制修改)**：用于把 d8 打印出的字节码文本再还原/重建为 JavaScript 近似源码。


---

## 快速开始

### 1. 获取对应版本 d8

前往 Releases 页面，选择与你的目标 `.jsc` 生成环境 **相同的 V8 版本号**。 （如果没有找到，请发起 Issues ） 
每个版本下提供：
- `d8`：Linux 64-bit 可执行文件
- `d8.exe`：Windows 64-bit 可执行文件

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

- 不同 V8 版本的 Bytecode 指令集、寄存槽布局、Handlers 表结构可能不同，请务必使用 **匹配版本** 的 d8。
- 由于没有node环境，所有node函数都将被标注为<unknown>
- 如果输出异常，请：
  1. 再次核对版本；
  2. 若仍有异常，请提起 Issues
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


# English

简体中文 | [English](#english)

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
- `d8`: Linux 64‑bit executable
- `d8.exe`: Windows 64‑bit executable

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

- V8 bytecode opcodes, register/slot layouts, and handler table structures vary across versions. Always use a **matching** d8 build.
- Because there is no Node.js runtime environment here, all Node‑specific functions will be labeled as `<unknown>`.
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
