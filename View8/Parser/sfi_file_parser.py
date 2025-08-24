from Parser.shared_function_info import SharedFunctionInfo, CodeLine
from parse import parse
import re
import traceback
from typing import List, Optional, Tuple, Dict, Set

# 全局：解析结果
all_functions: Dict[str, SharedFunctionInfo] = {}
repeat_last_line = False

# 详细错误定位
current_line_number = 0
current_file_content: List[str] = []
current_file_name: str = ""

# 预扫描：地址(整数) -> 数组（数字列表）
FIXED_ARRAYS: Dict[int, List[int]] = {}

VERBOSE = False


def set_repeat_line_flag(flag: bool):
    global repeat_last_line
    repeat_last_line = flag


def read_file_with_best_encoding(file_path: str) -> List[str]:
    encodings = ["utf-8", "utf-8-sig", "gbk", "cp936", "cp1252", "latin-1"]
    last_error: Optional[Exception] = None
    for enc in encodings:
        try:
            with open(file_path, "r", encoding=enc, errors="strict") as f:
                return f.readlines()
        except UnicodeDecodeError as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue

    print(
        f"WARNING: Could not decode file with common encodings. "
        f"Falling back to utf-8 with replacement. Last error: {last_error}"
    )
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


def normalize_addr(addr_str: str) -> Optional[int]:
    s = addr_str.strip().lower().removeprefix("0x")
    if not s:
        return 0  # Handle all-zero addresses
    try:
        return int(s, 16)
    except ValueError:
        return None


def collect_fixed_arrays(file_path: str):
    """
    预扫描所有 Start FixedArray 块，建立地址(整数) -> 数组 的映射。
    仅收集纯数字元素（SMI），范围 i-j: v 会填充每个索引。
    """
    global FIXED_ARRAYS
    FIXED_ARRAYS.clear()

    lines = read_file_with_best_encoding(file_path)
    i = 0
    N = len(lines)

    rx_addr = re.compile(r'^\s*((?:0x)?[0-9a-fA-F]+):\s*\[FixedArray\]')
    rx_len = re.compile(r'^\s*-\s*length:\s*(\d+)\s*$')
    rx_single = re.compile(r'^\s*(\d+)\s*:\s*(-?\d+)\s*$')
    rx_range = re.compile(r'^\s*(\d+)\s*-\s*(\d+)\s*:\s*(-?\d+)\s*$')

    while i < N:
        if lines[i].strip() == "Start FixedArray":
            # 地址行
            i += 1
            if i >= N:
                break
            m_addr = rx_addr.match(lines[i].strip())
            if not m_addr:
                # 往后探两行
                found = False
                for j in range(i, min(i + 3, N)):
                    m2 = rx_addr.match(lines[j].strip())
                    if m2:
                        i = j
                        m_addr = m2
                        found = True
                        break
                if not found:
                    # 跳过到 End FixedArray
                    while i < N and lines[i].strip() != "End FixedArray":
                        i += 1
                    i += 1
                    continue
            addr_int = normalize_addr(m_addr.group(1))
            length = None
            arr: Dict[int, int] = {}

            # 找 length
            j = i + 1
            while j < N:
                l = lines[j].strip()
                m_len = rx_len.match(l)
                if m_len:
                    length = int(m_len.group(1))
                    j += 1
                    break
                j += 1

            if length is None:
                # 跳到 End
                while j < N and lines[j].strip() != "End FixedArray":
                    j += 1
                i = j + 1
                continue

            # 解析条目
            while j < N:
                l = lines[j].strip()
                if l == "End FixedArray":
                    break
                m_r = rx_range.match(l)
                if m_r:
                    s = int(m_r.group(1)); e = int(m_r.group(2)); v = int(m_r.group(3))
                    for k in range(s, e + 1):
                        if 0 <= k < length:
                            arr[k] = v
                    j += 1
                    continue
                m_s = rx_single.match(l)
                if m_s:
                    idx = int(m_s.group(1)); v = int(m_s.group(2))
                    if 0 <= idx < length:
                        arr[idx] = v
                    j += 1
                    continue
                # 其它行忽略
                j += 1

            if addr_int is not None:
                out = [0] * length
                for k, v in arr.items():
                    out[k] = v
                FIXED_ARRAYS[addr_int] = out

            # 跳过 End FixedArray
            while j < N and lines[j].strip() != "End FixedArray":
                j += 1
            i = j + 1
            continue

        i += 1


def get_next_line(file_path: str):
    """
    逐行（去空行）生成器，支持回推一行。
    调用 parse_file 时会先 collect_fixed_arrays。
    """
    global current_line_number, current_file_content, current_file_name
    current_file_name = file_path
    try:
        current_file_content = read_file_with_best_encoding(file_path)
    except Exception as e:
        log_error(f"Fatal error parsing file '{file_path}': {e}")
        print(f"Traceback:\n{traceback.format_exc()}")
        yield None
        return

    for line_num, raw in enumerate(current_file_content, 1):
        current_line_number = line_num
        line = raw.strip()
        if not line:
            continue
        yield line
        if repeat_last_line:
            set_repeat_line_flag(False)
            yield line
    yield None


def log_error(message: str, context: str = ""):
    global current_line_number, current_file_content, current_file_name
    location = f"{current_file_name}:{current_line_number}" if current_file_name else f"line {current_line_number}"
    print(f"ERROR at {location}: {message}")
    if context:
        print(f"Context: {context}")

    if current_file_content and 1 <= current_line_number <= len(current_file_content):
        start = max(1, current_line_number - 2)
        end = min(len(current_file_content), current_line_number + 2)
        print("Source context:")
        for i in range(start, end + 1):
            prefix = ">>> " if i == current_line_number else "    "
            print(f"{prefix}{i:4d}: {current_file_content[i - 1].rstrip()}")
    print()


# --------------------------
# Bytecode 解析
# --------------------------

def parse_bytecode_line(line: str) -> Optional[CodeLine]:
    m = re.search(r"^[^@]*@ +(\d+) : ([0-9a-fA-F ]+)\s+([A-Za-z_][A-Za-z0-9._]*(?:.*))$", line)
    if m:
        offset, opcode, inst = m.groups()
        try:
            line_num = int(offset.strip())
            return CodeLine(opcode=opcode.strip(), line=line_num, inst=inst.strip())
        except ValueError:
            log_error(f"Could not parse offset '{offset}' in bytecode line", line)
            return None

    m2 = re.search(r"@ +(\d+)", line)
    if m2:
        try:
            line_num = int(m2.group(1))
            return CodeLine(opcode="", line=line_num, inst="// placeholder: " + line.strip())
        except ValueError:
            pass
    return None


def parse_bytecode(first_line: str, lines) -> List[CodeLine]:
    code_list: List[CodeLine] = []
    current_line = first_line

    while current_line is not None and " @ " in current_line:
        parsed = parse_bytecode_line(current_line)
        if parsed is not None:
            code_list.append(parsed)
        current_line = next(lines)

    if current_line and " @ " not in current_line:
        set_repeat_line_flag(True)

    # 以偏移排序并去重
    code_list.sort(key=lambda x: x.line_num)
    uniq: List[CodeLine] = []
    seen: Set[int] = set()
    for c in code_list:
        if c.line_num in seen:
            continue
        seen.add(c.line_num)
        uniq.append(c)
    return uniq


# --------------------------
# 常量池与嵌套块解析
# --------------------------

def _parse_string_value(text: str) -> str:
    """
    把 <String[n]: #name> 或 <String[n]: name> 标准化为 "name"
    """
    t = text.strip().rstrip(">")
    if "# " in t:
        t = t.split("#", 1)[-1].strip()
    elif "#" in t:
        t = t.split("#", 1)[-1].strip()
    else:
        if ":" in t:
            t = t.split(":", 1)[-1].strip()
    t = t.replace('"', '\\"')
    return f'"{t}"'


def _inline_fixed_array_from_val(raw_val: str) -> Optional[str]:
    """
    尝试从值文本中提取 FixedArray 地址，并将其内联为 JS 数组字面量。
    支持：..., 0x... <FixedArray[N]> 这种形态。
    """
    m = re.search(r'(0x[0-9a-fA-F]+)\s*<FixedArray\[\d+\]>', raw_val)
    if not m:
        return None
    addr = normalize_addr(m.group(1))
    if addr is None:
        return None
    nums = FIXED_ARRAYS.get(addr)
    if nums is None:
        return None
    return "[" + ", ".join(str(n) for n in nums) + "]"


def skip_block(lines, start_line: str):
    """
    跳过一整个 Start ... / End ... 结构块（ObjectBoilerplateDescription、ArrayBoilerplateDescription、FixedArray等）。
    前置：当前 start_line 为以 "Start " 开头的行。
    """
    kind = start_line.strip().split(" ", 1)[-1]
    end_marker = f"End {kind}"
    while True:
        try:
            l = next(lines)
        except StopIteration:
            break
        if l.strip() == end_marker:
            break


def _parse_const_value_from_single(address: Optional[str], value: str, lines, func_name: str) -> str:
    val = value.strip()

    if address:
        if val.startswith("<String"):
            return _parse_string_value(val)

        # 优先尝试内联 FixedArray
        inline = _inline_fixed_array_from_val(val)
        if inline is not None:
            return inline

        if val.startswith("<SharedFunctionInfo"):
            # 只有在下一行真开始时，才递归解析嵌套 SFI
            try:
                peek = next(lines)
                if peek == "Start SharedFunctionInfo":
                    nested_label = val.split(" ", 1)[-1].rstrip('> ') if " " in val else ""
                    nested_name = parse_shared_function_info(lines, nested_label, func_name)
                    return nested_name
                else:
                    set_repeat_line_flag(True)
            except StopIteration:
                pass
            short = val.split()[-1].rstrip(">") if " " in val else "unknown"
            return f"func_ref_{short}"

        if val.startswith("<ArrayBoilerplateDescription"):
            inline = _inline_fixed_array_from_val(val)
            if inline is not None:
                return inline
            return "[]"

        if val.startswith("<ObjectBoilerplateDescription"):
            # 对象模板块会在常量池之后详细展开，这里只给占位 "{}"
            return "{}"

        if val.startswith("<FixedArray"):
            inline = _inline_fixed_array_from_val(val)
            if inline is not None:
                return inline
            return "[]"

        if val.startswith("<Odd Oddball"):
            return "null"

        # 其它带标签的，保留右侧可读部分
        return val.rstrip('>').split(" ", 1)[-1]

    # 无地址：纯字面量/数字/布尔/Corrupted 文本
    return val


def parse_const_pool(line: str, lines, func_name: str) -> List[str]:
    """
    解析“Constant pool (size = N)”：
    - 持续采集到恰好 N 个索引被赋值；
    - 期间可能穿插多个子块：Start SharedFunctionInfo、Start ObjectBoilerplateDescription、
      Start ArrayBoilerplateDescription、Start FixedArray 等，均在本函数内消费完后继续；
    - 只有当 N 个项都收齐，或遇到父块的 Start BytecodeArray/Handler Table/Source Position Table 才结束。
    """
    m = re.search(r"Constant pool\s*\(size\s*=\s*(\d+)\)", line)
    if not m:
        return []
    size = int(m.group(1))
    if size <= 0:
        return []

    const_list: List[Optional[str]] = [None] * size
    assigned = 0

    rx_range = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*:\s*(.+)$")
    rx_single = re.compile(r"^\s*(\d+)\s*:\s*(0x[0-9a-fA-F]+\s+)?(.+)$")

    while True:
        try:
            l = next(lines)
        except StopIteration:
            break

        s = l.strip()

        # 采满 N 个索引则停止；把这行回推给上层（通常是 Handler Table/Bytecode 开始）
        if assigned >= size:
            set_repeat_line_flag(True)
            break

        # 子块：递归/跳过后继续
        if s == "Start SharedFunctionInfo":
            parse_shared_function_info(lines, f"nested_{len(all_functions)}", func_name)
            continue
        if s.startswith("Start ObjectBoilerplateDescription") or s.startswith("Start ArrayBoilerplateDescription") or s.startswith("Start FixedArray"):
            skip_block(lines, s)
            continue

        # 父块边界（如果未收满，只能结束）
        if s.startswith("Start BytecodeArray") or s.startswith("Handler Table") or s.startswith("Source Position Table") or s == "End SharedFunctionInfo":
            set_repeat_line_flag(True)
            break

        # 范围行
        m_range = rx_range.match(s)
        if m_range:
            si = int(m_range.group(1)); ei = int(m_range.group(2))
            raw_val = m_range.group(3).strip()
            for idx in range(si, ei + 1):
                if 0 <= idx < size and const_list[idx] is None:
                    const_list[idx] = raw_val
                    assigned += 1
            continue

        # 单索引
        m_single = rx_single.match(s)
        if m_single:
            idx = int(m_single.group(1))
            addr = m_single.group(2)
            val = m_single.group(3)
            if 0 <= idx < size and const_list[idx] is None:
                const_list[idx] = _parse_const_value_from_single(addr, val, lines, func_name)
                assigned += 1
            continue

        # 其它行忽略（map/length/...）
        continue

    # 填补空位
    for i in range(size):
        if const_list[i] is None:
            const_list[i] = ""

    return const_list


# --------------------------
# 异常表与其它字段
# --------------------------

def parse_exception_table_line(line: str) -> Tuple[int, List[int]]:
    from_, to_, key, _ = parse("({},{})  -> {} ({}", line)
    return int(key), [int(from_), int(to_)]


def parse_handler_table(line: str, lines) -> Dict[int, List[int]]:
    if "size = 0" in line:
        return {}
    exception_table: Dict[int, List[int]] = {}
    nxt = next(lines)
    if nxt is None:
        return {}
    while True:
        line = next(lines)
        if line is None or " -> " not in line:
            break
        key, value = parse_exception_table_line(line)
        exception_table[key] = value
    set_repeat_line_flag(True)
    return exception_table


def parse_parameter_count(line: str) -> int:
    return int(parse("Parameter count {}", line)[0])


def parse_register_count(line: str) -> int:
    return int(parse("Register count {}", line)[0])


def parse_address(line: str) -> str:
    return parse("{}: [{}] in {}", line)[0]


# --------------------------
# SharedFunctionInfo 解析
# --------------------------

def parse_shared_function_info(lines, name: str, declarer: Optional[str] = None) -> str:
    sfi = SharedFunctionInfo()
    sfi.declarer = declarer
    sfi.name = 'func_unknown'
    # 记录 ScopeInfo 地址
    sfi.scope_info_addr = None
    sfi.outer_scope_info_addr = None
    try:
        while True:
            try:
                line = next(lines)
            except StopIteration:
                break

            if line is None or line == "End SharedFunctionInfo":
                break

            # 先匹配 outer，再匹配 scope，避免 “scope info” 命中 “outer scope info” 子串
            m_outer = re.search(r'^\s*-\s*outer scope info:\s*(0x[0-9a-fA-F]+)', line)
            if not m_outer:
                # 宽松兜底（不以 - 开头的行）
                m_outer = re.search(r'\bouter scope info:\s*(0x[0-9a-fA-F]+)', line)
            if m_outer:
                sfi.outer_scope_info_addr = m_outer.group(1)
                continue

            m_scope = re.search(r'^\s*-\s*scope info:\s*(0x[0-9a-fA-F]+)', line)
            if not m_scope:
                # 宽松兜底：禁止匹配 'outer scope info' 中的 'scope info' 子串
                m_scope = re.search(r'(?<!outer )scope info:\s*(0x[0-9a-fA-F]+)', line)
            if m_scope:
                sfi.scope_info_addr = m_scope.group(1)
                continue

            if line == "Start SharedFunctionInfo":
                nested_name = f"nested_{len(all_functions)}"
                parse_shared_function_info(lines, nested_name, sfi.name)
                continue

            if "Parameter count" in line:
                sfi.argument_count = parse_parameter_count(line)
                continue

            if "Register count" in line:
                sfi.register_count = parse_register_count(line)
                continue

            if "Constant pool" in line:
                sfi.const_pool = parse_const_pool(line, lines, sfi.name)
                continue

            if "Handler Table" in line:
                sfi.exception_table = parse_handler_table(line, lines)
                continue

            if "@    0 : " in line:
                sfi.code = parse_bytecode(line, lines)
                continue

            if "[SharedFunctionInfo]" in line or "[BytecodeArray]" in line:
                address = parse_address(line)
                sfi.name = f'func_{(name or "unknown")}_{address}'
                continue

        # 归一化
        if sfi.argument_count is None:
            sfi.argument_count = 0
        if sfi.register_count is None:
            sfi.register_count = 0
        if sfi.const_pool is None:
            sfi.const_pool = []
        if sfi.exception_table is None:
            sfi.exception_table = {}
        if sfi.code is None:
            sfi.code = []

        # 补齐偏移占位，便于后续处理
        if sfi.code:
            offs = [c.line_num for c in sfi.code]
            if offs:
                exist = set(offs)
                for off in range(min(offs), max(offs) + 1):
                    if off not in exist:
                        sfi.code.append(CodeLine(opcode="", line=off, inst="// placeholder"))
                sfi.code.sort(key=lambda x: x.line_num)

        all_functions[sfi.name] = sfi
        return sfi.name

    except Exception as e:
        log_error(f"Error parsing SharedFunctionInfo '{name}': {e}")
        print(f"Traceback:\n{traceback.format_exc()}")
        # 兜底保存
        if sfi.argument_count is None:
            sfi.argument_count = 0
        if sfi.register_count is None:
            sfi.register_count = 0
        if sfi.const_pool is None:
            sfi.const_pool = []
        if sfi.exception_table is None:
            sfi.exception_table = {}
        if sfi.code is None:
            sfi.code = []
        all_functions[sfi.name] = sfi
        return sfi.name


# --------------------------
# 入口
# --------------------------

def parse_file(file_path: str = "test.txt") -> Dict[str, SharedFunctionInfo]:
    try:
        collect_fixed_arrays(file_path)
        lines = get_next_line(file_path)
        for line in lines:
            if line is None:
                break
            if line == "Start SharedFunctionInfo":
                parse_shared_function_info(lines, "start")
        return all_functions
    except Exception as e:
        log_error(f"Fatal error parsing file '{file_path}': {e}")
        print(f"Traceback:\n{traceback.format_exc()}")
        return all_functions


if __name__ == '__main__':
    parse_file()