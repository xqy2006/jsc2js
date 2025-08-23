import re
from Simplify.function_context_stack import function_context_stack

# ========== 精准调试开关（只对目标函数生效） ==========
TARGET_DEBUG_ADDR = "0x708001a5f95"   # 只调试这个 SFI 地址
DEBUG_LOG_FILE = f"debug_{TARGET_DEBUG_ADDR}.log"

def _debug_enabled_for(sfi_name: str) -> bool:
    return TARGET_DEBUG_ADDR in (sfi_name or "")

def _dbg_write(msg: str):
    try:
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg.rstrip("\n") + "\n")
    except Exception:
        pass

def _dbg_reset():
    try:
        with open(DEBUG_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass
# ======================================================

# 基于 ScopeInfo 的父子关系与上下文编号映射（稳定分配）
SCOPEINFO_PARENT = {}     # child_scope_info -> outer_scope_info
SCOPE_CTXID = {}          # scope_info -> stable ctx id (int > 0)
_SCOPEINFO_INIT = False

# 全局槽位环境：记录已知的槽位右值，供跨函数内联
# key: (ctx_id:int, slot:int) -> value:str（已做寄存器常量替换后的字符串）
SCOPE_SLOT_ENV = {}

def _hex_to_int(addr: str) -> int:
    try:
        return int(addr, 16)
    except Exception:
        return 0


def _init_scopeinfo_graph():
    """
    从 all_functions 收集 scope info 依赖图，并进行稳定编号：
    - 收集所有 scope_info 与 outer_scope_info 地址；
    - 按十六进制地址升序分配 1..N 的稳定 ID 到 SCOPE_CTXID。
    """
    global _SCOPEINFO_INIT
    if _SCOPEINFO_INIT:
        return
    try:
        try:
            from Parser.sfi_file_parser import all_functions
        except Exception:
            from sfi_file_parser import all_functions
    except Exception:
        return

    addrs = set()
    for sfi in list(all_functions.values()):
        si = getattr(sfi, "scope_info_addr", None)
        oi = getattr(sfi, "outer_scope_info_addr", None)
        if si is not None:
            SCOPEINFO_PARENT[si] = oi  # oi 可以为 None（根）
            addrs.add(si)
        if oi is not None:
            addrs.add(oi)

    # 稳定编号：按地址排序固定映射
    sorted_addrs = sorted(addrs, key=_hex_to_int)
    for idx, addr in enumerate(sorted_addrs, start=1):
        SCOPE_CTXID[addr] = idx

    _SCOPEINFO_INIT = True


def _ctx_for_scopeinfo(scope_info: str) -> int:
    """
    返回某个 scope_info 的稳定编号。
    解析阶段已一次性分配，正常不会缺；若缺，追加为新的最大编号（不影响既有映射）。
    """
    if scope_info in SCOPE_CTXID:
        return SCOPE_CTXID[scope_info]
    new_id = (max(SCOPE_CTXID.values()) + 1) if SCOPE_CTXID else 1
    SCOPE_CTXID[scope_info] = new_id
    return new_id


def _ascend_scopeinfo(si: str | None, steps: int) -> str | None:
    """沿 ScopeInfo 链上溯 steps 层，返回祖先 scope_info。"""
    cur = si
    for _ in range(steps):
        if not cur:
            return None
        cur = SCOPEINFO_PARENT.get(cur)
    return cur


def _current_ctx_for_function(sfi) -> int:
    """
    决定“CURRENT 应替换成哪个编号”的策略（默认用本函数编号）：
    - CURRENT（纯） → 使用自身 scope_info 的编号；
    - 需要父/祖先编号的场景，在表达式解析中按需上溯。
    """
    si = getattr(sfi, "scope_info_addr", None)
    if si:
        return _ctx_for_scopeinfo(si)
    # 回退：旧逻辑
    return function_context_stack.get_func_context(sfi.name, getattr(sfi, "declarer", None))


class Register:
    def __init__(self, value, init_index, was_overwritten=False):
        self.value = value
        self.was_overwritten = was_overwritten
        self.all_initialized_index = [init_index]


def get_block_type(idx, lines):
    if idx == 0:
        return "function"
    first_block_line = lines[idx - 1].decompiled
    block_types = {"try": "try", "catch": "catch", "while": "loop", "switch": "case", "case": "case", "if": "if", "else": "else"}
    for keyword, block_type in block_types.items():
        if keyword in first_block_line:
            return block_type
    return "unknown"


def reg_is_constant(reg, value):
    if reg.startswith(("ACCU", "CASE_")):
        return True
    if re.search(r"[\w\]]\(", value):
        return False
    if re.search(r"^[\(]*(Scope|ConstPool|<|true|false|Undefined|Null|null|[+-]?\d)", value):
        return True
    if re.search(r"^[ra]\d+\[[\(]*ConstPool\[\d+\]", value):
        return True
    return False


def get_context_idx_from_var(var):
    if var.was_overwritten:
        return
    match = re.match(r"Scope\[(\d+)\]", var.value)
    if match:
        return int(match.group(1))
    return None


def is_reg_defined_in_reg_value(reg, value):
    reg_len = len(reg)
    idx = value.find(reg)
    while idx != -1:
        if idx + reg_len == len(value) or not value[idx + reg_len].isdigit():
            return True
        idx = value.find(reg, idx + 1)


def create_loop_reg_scope(prev_reg_scope):
    reg_scope = {k: Register("", v.all_initialized_index[0], True) for k, v in prev_reg_scope.items() if not isinstance(v, int)}
    reg_scope["current_context"] = prev_reg_scope["current_context"]
    return reg_scope


def close_loop_reg_scope(prev_reg_scope, reg_scope):
    for k, v in reg_scope.items():
        if isinstance(v, int):
            continue
        if v.was_overwritten and len(v.all_initialized_index) > 1 and k in prev_reg_scope and not prev_reg_scope[k].was_overwritten:
            prev_reg_scope[k].was_overwritten = True
            prev_reg_scope[k].all_initialized_index += reg_scope[k].all_initialized_index[1:]


# 判断一个右值是否“简单可安全内联”：不包含括号(，避免把函数调用结果当常量
_SIMPLE_VALUE_RE = re.compile(r'^[A-Za-z0-9_\[\]\."\'$:<>-]+$')  # 允许属性取值、字面量、ConstPool[...]、地址字符串等
def _is_simple_slot_value(val: str) -> bool:
    val = (val or "").strip()
    if not val:
        return False
    if "(" in val or ")" in val:
        return False
    return bool(_SIMPLE_VALUE_RE.match(val))


class SimplifyCode:
    def __init__(self, code, sfi, ctx_id):
        self.code = code
        self.line_index = 0
        self.tab_level = 0
        self.sfi = sfi
        self.cur_ctx_id = ctx_id
        self.cur_scope_info = getattr(sfi, "scope_info_addr", None)
        self.outer_scope_info = getattr(sfi, "outer_scope_info_addr", None)
        self._is_target = _debug_enabled_for(sfi.name)

    def get_next_line(self):
        self.line_index += 1
        if self.line_index >= len(self.code):
            print("Error decompiling {self.sfi.name}, no more lines.")
        line_obj = self.code[self.line_index]
        return line_obj.translated

    def add_simplified_line(self, line):
        self.code[self.line_index].decompiled = '\t' * self.tab_level + line if line else ""

    def change_context(self, line, reg_scope):
        # 语言级 Push/PopContext 不改变我们分配的编号体系
        if "PushContext" in line:
            if self._is_target:
                _dbg_write(f"[CTX] PushContext at line {self.line_index}, keep cur_ctx_id={self.cur_ctx_id}")
            return f"ACCU = Scope[CURRENT-1]"
        if self._is_target:
            _dbg_write(f"[CTX] PopContext at line {self.line_index}, keep cur_ctx_id={self.cur_ctx_id}")
        return f"ACCU = Scope[CURRENT]"

    def _resolve_scope_expr_to_index(self, expr, *, prefer_outer_for_slot: bool, reg_scope, prev_reg_scope):
        """
        将 Scope[...] 表达式解析成具体编号：
        - CURRENT → 默认返回本函数编号（self.cur_ctx_id）；但如果 prefer_outer_for_slot=True，则返回 outer 的编号
        - CURRENT-n → 从本函数 scope_info 出发向上 n 层（用 ScopeInfo 链），拿到祖先编号
        - 解析失败时，不返回 0，而是留在 CURRENT 以避免错误降级（由调用方决定是否替换）
        """
        expr = expr.strip()

        # 纯数字：直接返回
        if re.fullmatch(r"\d+", expr):
            idx = int(expr)
            if self._is_target:
                _dbg_write(f"[RESOLVE] '{expr}' -> {idx} (numeric)")
            return idx, True  # 一定可替换

        # 解析 steps
        if "-" in expr:
            base, steps_s = expr.split("-", 1)
            steps = int(steps_s.strip())
        else:
            base, steps = expr, 0
        base = base.strip()

        if base == "CURRENT":
            if steps == 0:
                # 有槽位访问时，偏向父作用域编号（读场景）
                if prefer_outer_for_slot and self.outer_scope_info:
                    idx = _ctx_for_scopeinfo(self.outer_scope_info)
                    if self._is_target:
                        _dbg_write(f"[RESOLVE] 'CURRENT'(slot) -> {idx} (outer scope {self.outer_scope_info})")
                    return idx, True
                idx = self.cur_ctx_id or 0
                if self._is_target:
                    _dbg_write(f"[RESOLVE] 'CURRENT' -> {idx} (cur_ctx_id)")
                return idx, bool(self.cur_ctx_id)
            # 向上 steps 层
            target_si = _ascend_scopeinfo(self.cur_scope_info, steps)
            if not target_si:
                if self._is_target:
                    _dbg_write(f"[RESOLVE] 'CURRENT-{steps}' -> (no ancestor), keep as-is")
                return 0, False
            idx = _ctx_for_scopeinfo(target_si)
            if self._is_target:
                _dbg_write(f"[RESOLVE] 'CURRENT-{steps}' -> {idx} (scope_info {target_si})")
            return idx, True

        # 其它形式（如 r1-2）回退旧逻辑：以当前编号为起点上溯
        start_ctx = reg_scope['current_context']
        from Simplify.function_context_stack import function_context_stack as FCS
        idx = FCS.get_context(start_ctx, steps)
        if self._is_target:
            _dbg_write(f"[RESOLVE] '{expr}' (fallback from {start_ctx}, steps {steps}) -> {idx}")
        return idx, start_ctx != 0

    def replace_scope_stack_with_idx(self, line, reg_scope, prev_reg_scope):
        """
        区分读写：
        - 若本行以 'Scope[...][slot] =' 作为赋值左侧（LHS），则 prefer_outer_for_slot=False（用当前编号）；
        - 其他出现（RHS/调用等）若紧跟 [slot]，则 prefer_outer_for_slot=True（偏向父编号）。
        """
        # 匹配整行 LHS：Scope[...][n] =
        lhs_match = re.match(r"^\s*Scope\[([^\]]+)\]\[(\d+)\]\s*=", line)
        lhs_span_start = lhs_match.start() if lhs_match else -1

        # 扩展匹配：捕获 Scope[...] 及可选的 [n]
        pattern = re.compile(r"Scope\[([^\]]+)\](\[(\d+)\])?")

        # 调试：预扫描
        if self._is_target:
            for m in pattern.finditer(line):
                e = m.group(1)
                has_slot = m.group(2) is not None
                is_lhs = bool(lhs_match) and (m.start() == lhs_span_start) and has_slot
                idx, certain = self._resolve_scope_expr_to_index(
                    e, prefer_outer_for_slot=(has_slot and not is_lhs), reg_scope=reg_scope, prev_reg_scope=prev_reg_scope
                )
                _dbg_write(f"[PRE-REPL] line {self.line_index}: Scope[{e}]{m.group(2) or ''} -> {idx}, certain={certain}, has_slot={has_slot}, is_lhs={is_lhs}")

        def repl(match):
            scope_expr = match.group(1)
            has_slot = match.group(2) is not None
            slot_suffix = match.group(2) or ""

            # 判断这次匹配是否是 LHS 的 Scope[...]（仅当行首形态匹配，且该匹配就是那一段）
            is_lhs = bool(lhs_match) and (match.start() == lhs_span_start) and has_slot

            idx, certain = self._resolve_scope_expr_to_index(
                scope_expr, prefer_outer_for_slot=(has_slot and not is_lhs), reg_scope=reg_scope, prev_reg_scope=prev_reg_scope
            )
            # 解析失败或为 0：保持原样
            if not certain or idx == 0:
                if self._is_target:
                    _dbg_write(f"[KEEP   ] Scope[{scope_expr}]{slot_suffix} (uncertain or 0), keep")
                return f"Scope[{scope_expr}]{slot_suffix}"
            if self._is_target:
                _dbg_write(f"[REPLACE] Scope[{scope_expr}]{slot_suffix} -> Scope[{idx}]{slot_suffix} (is_lhs={is_lhs})")
            return f"Scope[{idx}]{slot_suffix}"

        out = pattern.sub(repl, line)

        # 如本行是 Scope[...] 赋值，记录替换前后
        if self._is_target and "Scope[" in line and "=" in line:
            _dbg_write(f"[LINE-BEFORE] {line}")
            _dbg_write(f"[LINE-AFTER ] {out}")

        return out

    def replace_reg_with_constant(self, line, reg_scope):
        def replace_reg(match):
            reg = match.group(1)
            if reg not in reg_scope:
                return reg
            if not reg_scope[reg].was_overwritten:
                self.code[reg_scope[reg].all_initialized_index[0]].visible = False
                return reg_scope[reg].value
            for idx in reg_scope[reg].all_initialized_index:
                self.code[idx].visible = True
            return reg
        return re.sub(r"(ACCU|CASE_\d+|[ra]\d+)", replace_reg, line)

    def _inline_scope_slot_reads_in_text(self, text: str) -> str:
        """
        在一段文本里，把已知的 Scope[num][slot] 替换成记录的右值。
        仅替换 RHS，调用方需保证不是 LHS 的那一处。
        """
        def repl(m):
            num = int(m.group(1)); slot = int(m.group(2))
            val = SCOPE_SLOT_ENV.get((num, slot))
            if val is None:
                return m.group(0)
            if self._is_target:
                _dbg_write(f"[INLINE ] Scope[{num}][{slot}] -> {val}")
            return val
        return re.sub(r"Scope\[(\d+)\]\[(\d+)\]", repl, text)

    def simplify_line(self, line, reg_scope, prev_reg_scope, overwritten_regs):
        # 处理上下文切换
        if "PopContext" in line or "PushContext" in line:
            line = self.change_context(line, reg_scope)

        # 先做 Scope[CURRENT]/[CURRENT-1] 等解析、并区分读/写
        original_line = line
        line = self.replace_scope_stack_with_idx(line, reg_scope, prev_reg_scope)

        # 赋值行？
        m_assign = re.match(r"^\s*(ACCU|CASE_\d+|[ra]\d+|Scope\[\d+\]\[\d+\])\s*=\s*(.+)$", line)
        if not m_assign:
            # 非赋值行：先内联已知槽位，再替换寄存器常量
            line2 = self._inline_scope_slot_reads_in_text(line)
            out_line = self.replace_reg_with_constant(line2, reg_scope)
            if self._is_target and out_line != original_line:
                _dbg_write(f"[CONST  ] line {self.line_index}: {original_line}  ==>  {out_line}")
            return out_line

        # 是赋值：拆 LHS/RHS
        lhs = m_assign.group(1)
        rhs = m_assign.group(2).strip()

        # 如果 RHS 里有 Scope[num][slot] 读取，先内联它们（RHS 才允许）
        rhs = self._inline_scope_slot_reads_in_text(rhs)

        # 然后做寄存器常量替换
        rhs2 = self.replace_reg_with_constant(rhs, reg_scope)

        # 记录寄存器生命周期
        if lhs in reg_scope:
            del reg_scope[lhs]
        if lhs in prev_reg_scope:
            prev_reg_scope[lhs].was_overwritten = True
            overwritten_regs[lhs] = self.line_index
        for k, v in reg_scope.items():
            if type(v) == int:
                continue
            if is_reg_defined_in_reg_value(lhs, v.value):
                reg_scope[k].was_overwritten = True
        if reg_is_constant(lhs, rhs2):
            reg_scope[lhs] = Register(rhs2, self.line_index)

        # 如果 LHS 是 Scope[num][slot]，并且 RHS 是“简单可内联”的表达式，记录到全局槽位环境
        m_lhs_slot = re.match(r"^\s*Scope\[(\d+)\]\[(\d+)\]\s*$", lhs)
        if m_lhs_slot:
            num = int(m_lhs_slot.group(1)); slot = int(m_lhs_slot.group(2))
            if _is_simple_slot_value(rhs2):
                SCOPE_SLOT_ENV[(num, slot)] = rhs2
                if self._is_target:
                    _dbg_write(f"[ENVSET ] Scope[{num}][{slot}] = {rhs2}")
            else:
                # 复杂右值，不记录（也可选择删除已有记录保证保守正确）
                if (num, slot) in SCOPE_SLOT_ENV:
                    if self._is_target:
                        _dbg_write(f"[ENVDEL ] Scope[{num}][{slot}] (rhs not simple)")
                    del SCOPE_SLOT_ENV[(num, slot)]

        return f"{lhs} = {rhs2}"

    def simplify_block(self, prev_reg_scope):
        block_type = get_block_type(self.line_index, self.code)

        reg_scope = prev_reg_scope.copy() if block_type != "loop" else create_loop_reg_scope(prev_reg_scope)
        overwritten_regs = {}

        self.add_simplified_line("{")
        self.tab_level += 1

        while (line := self.get_next_line()) != "}":
            if self._is_target and self.line_index == 1:
                # 首行进入时，dump 一次翻译原文，便于对比
                _dbg_write("[DUMP-TRANSLATED-BEGIN]")
                for i, c in enumerate(self.code):
                    if getattr(c, "translated", None):
                        _dbg_write(f"{i:04d}: {c.translated}")
                _dbg_write("[DUMP-TRANSLATED-END]")
            if line == "{":
                self.simplify_block(prev_reg_scope | reg_scope)
                continue
            simplified = self.simplify_line(line, reg_scope, prev_reg_scope, overwritten_regs)
            self.add_simplified_line(simplified)

        self.tab_level -= 1
        self.add_simplified_line("}")

        if block_type == "loop":
            close_loop_reg_scope(prev_reg_scope, reg_scope)
        for k, v in overwritten_regs.items():
            prev_reg_scope[k].all_initialized_index.append(v)

        return


def simplify_translated_bytecode(sfi, code):
    # 初始化 ScopeInfo 图并稳定编号
    _init_scopeinfo_graph()

    # 计算本函数的 CURRENT 应用的稳定编号（默认用自己的 scope info）
    ctx_id = _current_ctx_for_function(sfi)

    # 记录旧兜底（不覆盖非 0）
    if sfi.declarer:
        function_context_stack.function_declarer.setdefault(sfi.name, sfi.declarer)
    function_context_stack.add_function_context(sfi.name, ctx_id, declarer=sfi.declarer or sfi.name)

    # 目标函数：重置日志并打印头信息
    if _debug_enabled_for(sfi.name):
        _dbg_reset()
        _dbg_write("===== DEBUG for target function start =====")
        _dbg_write(f"name={sfi.name}")
        _dbg_write(f"scope_info={getattr(sfi, 'scope_info_addr', None)}")
        _dbg_write(f"outer_scope_info={getattr(sfi, 'outer_scope_info_addr', None)}")
        _dbg_write(f"assigned cur_ctx_id={ctx_id}")
        si = getattr(sfi, "scope_info_addr", None)
        oi = getattr(sfi, "outer_scope_info_addr", None)
        if si:
            _dbg_write(f"SCOPE_CTXID[{si}]={SCOPE_CTXID.get(si)}")
        if oi:
            _dbg_write(f"SCOPE_CTXID[{oi}]={SCOPE_CTXID.get(oi)}")
        _dbg_write("==========================================")

    simpl = SimplifyCode(code, sfi, ctx_id)
    regs = {"current_context": ctx_id}
    simpl.simplify_block(regs)

    if _debug_enabled_for(sfi.name):
        _dbg_write("===== DEBUG end =====")

    if simpl.line_index != len(code) - 1:
        print(f"Warning! failed to decompile {sfi.name} stopped after {simpl.line_index}/{len(code)-1}")


def simplify_all_in_scope_order():
    """
    简化所有函数；采用稳定编号，同时按外层深度排序（父 → 子），
    以便尽早填充 SCOPE_SLOT_ENV，提升跨函数内联命中率。
    """
    _init_scopeinfo_graph()
    try:
        try:
            from Parser.sfi_file_parser import all_functions
        except Exception:
            from sfi_file_parser import all_functions
    except Exception:
        return

    # 计算每个函数的“outer 深度”
    def depth_of(si_addr: str | None) -> int:
        d, cur = 0, si_addr
        while cur is not None:
            cur = SCOPEINFO_PARENT.get(cur)
            d += 1
        return d

    funcs = list(all_functions.values())
    funcs.sort(key=lambda f: depth_of(getattr(f, "outer_scope_info_addr", None)))  # 父在前

    for sfi in funcs:
        simplify_translated_bytecode(sfi, sfi.code)