from Translate.translate_table import operands
from Translate.jump_blocks import convert_jumps_to_logical_flow
import re


class Jump:
    def __init__(self, jump_type, start, end):
        self.type = jump_type
        self.start = start
        self.end = end
        self.done = False


class SwitchJump(Jump):
    def __init__(self, jump_type, start, end, line, last_case):
        super().__init__(jump_type, start, end)
        self.case_line = line
        self.last_case_start = last_case


class TranslateBytecode:
    def __init__(self):
        self.offset = None
        self.operator = None
        self.args = None
        self.jump_table = None

    def add_exception_jumps(self, et):
        for catch_start, try_start in et.items():
            self.jump_table["Exception"][try_start[0]] = Jump(jump_type="Exception", start=try_start[0], end=catch_start)

    def add_jump_to_table(self, jump_type, start, end):
        table = self.jump_table.get(jump_type, None)
        if table is None:
            raise Exception(f"No Table for jump type {jump_type}")
        if table.get(start):
            raise Exception(f"Jump Table {jump_type} already has key {start}")
        table[start] = Jump(jump_type=jump_type, start=start, end=end)

    def add_int_switch_to_table(self, start, end, line, last):
        self.jump_table["IntSwitch"][start] = SwitchJump(jump_type="IntSwitch", start=start, end=end, line=line, last_case=last)

    def translate(self, name, code, exception_table):
        self.jump_table = {"Loop": {}, "Exception": {}, "Catch": {}, "IntSwitch": {}, "If": {}, "Jump": {}, "IfJSReceiver": {}}
        self.add_exception_jumps(exception_table)

        for line in code:
            # 跳过占位或注释（由解析器填充缺失偏移时写入）
            if not line.v8_instruction or line.v8_instruction.startswith("//"):
                continue
            # 仅接受以字母/下划线开头的助记符，避免把寄存器等当作操作码
            m = re.match(r"^(?:(?:[0-9a-fA-F]{2}\s+)+)?([A-Za-z_][A-Za-z0-9._]*)\b(?:\s+(.*))?$", line.v8_instruction.strip())
            if not m:
                continue

            self.offset = line.line_num
            self.operator = m.group(1)
            rest = m.group(2) or ""
            self.args = [arg.strip() for arg in rest.split(", ")] if rest else []

            handler = operands.get(self.operator)
            if not handler:
                # 未知操作符直接跳过，避免报错
                continue

            line.translated = handler(self)

        convert_jumps_to_logical_flow(name, code, self.jump_table)


def translate_bytecode(name, code, et):
    TranslateBytecode().translate(name, code, et)