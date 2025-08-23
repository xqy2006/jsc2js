from bisect import bisect_left

class CodeLine:
    def __init__(self, opcode="", line="", inst="", translated=""):
        self.v8_opcode = opcode
        self.line_num = line
        self.v8_instruction = inst
        self.translated = translated
        self.decompiled = ""
        self.visible = True


class JumpBlocks:
    def __init__(self, name, code, jump_table):
        self.function_name = name
        self.code_list = code
        # Map offsets to lines
        self.code = {i.line_num: i for i in code}
        # Use a sorted list of offsets for robust indexing and bisection
        self.code_offset = sorted(self.code.keys())
        self.jump_table = jump_table

    def snap_to_existing_offset(self, offset):
        # Return the closest existing offset at or before 'offset'.
        if not self.code_offset:
            return None
        idx = bisect_left(self.code_offset, offset)
        if idx == 0:
            # Before the first; snap to first
            return self.code_offset[0]
        if idx == len(self.code_offset):
            # Beyond last; snap to last
            return self.code_offset[-1]
        # If exact match
        if self.code_offset[idx] == offset:
            return offset
        # Otherwise snap to previous existing offset
        return self.code_offset[idx - 1]

    def get_line(self, offset):
        # Safely retrieve a CodeLine for a possibly missing offset by snapping.
        snapped = self.snap_to_existing_offset(offset)
        if snapped is None:
            return None
        return self.code.get(snapped, None)

    def jump_done(self, jmp):
        jmp.done = True
        if jmp.start in self.jump_table[jmp.type]:
            del self.jump_table[jmp.type][jmp.start]

    def get_relative_offset(self, offset, n):
        # return a relative line offset to a given offset using snapping and clamping
        if not self.code_offset:
            raise Exception("relative offset requested with empty code list")
        idx = bisect_left(self.code_offset, offset)
        # Snap to previous if not exact
        if idx >= len(self.code_offset):
            idx = len(self.code_offset) - 1
        elif self.code_offset[idx] != offset and idx > 0:
            idx -= 1
        new_idx = idx + n
        # Clamp
        if new_idx < 0:
            new_idx = 0
        if new_idx >= len(self.code_offset):
            new_idx = len(self.code_offset) - 1
        return self.code_offset[new_idx]

    def get_all_jump_list(self):
        # Combine all jumps from the jump tables into one list
        jump_list = [jmp for table in self.jump_table.values() for jmp in table.values()]

        # Adjust the end offset and leave starts as-is (we'll snap when using)
        for jmp in jump_list:
            if jmp.start == jmp.end:
                self.jump_done(jmp)
                continue

            if jmp.type not in {"Loop", "IntSwitch"}:
                # Shift the end to the previous instruction (snapped)
                jmp.end = self.get_relative_offset(jmp.end, -1)

        jump_list.sort(key=lambda x: (float(x.start), float(x.end)))
        return jump_list

    def close_section(self, start, end):
        # Snap to existing end, accounting for catch blocks
        for catch in self.jump_table["Catch"].values():
            if start < catch.start <= catch.end == catch.end:
                end = catch.start

        end_line = self.get_line(end)
        if not end_line:
            return end

        if "{" in end_line.translated:
            end_line.translated = "\n}\n" + end_line.translated
        else:
            end_line.translated += "\n}\n"

        return end_line.line_num

    def handle_switch_break(self, idx):
        break_jump = self.jump_table["Jump"].get(idx) or self.jump_table["If"].get(idx)
        if not break_jump:
            return
        start_line = self.get_line(break_jump.start)
        if not start_line:
            return
        start_line.translated += " break" if break_jump.type == "If" else "break"
        self.jump_done(break_jump)
        return break_jump.end

    def handle_break(self, range_start, range_end):
        statement = "break"
        end_jumps = set()
        jumps = list(self.jump_table["If"].values()) + list(self.jump_table["Jump"].values())
        for break_jump in jumps:
            if not range_start <= break_jump.start <= range_end < break_jump.end:
                continue

            start_line = self.get_line(break_jump.start)
            end_line = self.get_line(break_jump.end)
            if not start_line or not end_line:
                continue

            if break_jump.type == "If":
                start_line.translated += f" {statement}"
                end_jumps.add(end_line.line_num)
                self.jump_done(break_jump)
                continue

            if "{" in end_line.translated:
                start_line.translated = statement + start_line.translated
            else:
                start_line.translated += statement

            end_jumps.add(end_line.line_num)
            self.jump_done(break_jump)

        return end_jumps

    def handle_continue(self, range_start, range_end):
        try:
            near_loop_end = self.get_relative_offset(range_end, -4)
        except Exception:
            return

        statement = "continue"
        end_jumps = set()
        jumps = list(self.jump_table["If"].values()) + list(self.jump_table["Jump"].values())
        for continue_jump in jumps:
            if not (range_start <= continue_jump.start and near_loop_end <= continue_jump.end <= range_end):
                continue

            # Skip If jumps that end in a Jump
            if continue_jump.type == "If" and continue_jump.end in self.jump_table["Jump"]:
                continue

            start_line = self.get_line(continue_jump.start)
            end_line = self.get_line(continue_jump.end)
            if not start_line or not end_line:
                continue

            if continue_jump.type == "If":
                start_line.translated += f" {statement}"
                end_jumps.add(end_line.line_num)
                self.jump_done(continue_jump)
                continue

            if "{" in end_line.translated:
                start_line.translated = statement + start_line.translated
            else:
                start_line.translated += statement

            end_jumps.add(end_line.line_num)
            self.jump_done(continue_jump)

    def handle_loop(self, loop):
        if loop.type != 'Loop':
            return False

        start_line = self.get_line(loop.start)
        if not start_line:
            self.jump_done(loop)
            return True

        # Wrap loop start and end in while (true) { }
        start_line.translated = "while (true)\n{\n" + start_line.translated
        end_num = self.close_section(loop.start, loop.end)
        self.jump_done(loop)

        # Check for existence of loop break/continue
        # Since we shifted jumps one step back will send the shift also loop end
        self.handle_break(loop.start, self.get_relative_offset(loop.end, -1))
        self.handle_continue(loop.start, self.get_relative_offset(loop.end, -1))

        return True

    def handle_exception(self, try_jmp):
        if try_jmp.type != "Exception":
            return

        start_line = self.get_line(try_jmp.start)
        if not start_line:
            self.jump_done(try_jmp)
            return True

        # Wrap try block around the start and end of try_jmp
        start_line.translated = "try\n{\n" + start_line.translated

        # Find the corresponding catch jump
        catch_jump = self.jump_table["Jump"].get(try_jmp.end, None)
        if catch_jump:
            catch_start_line = self.get_line(catch_jump.start)
            catch_end_line = self.get_line(catch_jump.end)
            if catch_start_line:
                catch_start_line.translated += "\n}\ncatch\n{"
            if catch_end_line:
                catch_end_line.translated += "\n}\n"
            self.jump_table["Catch"][catch_jump.start] = catch_jump
            self.jump_done(catch_jump)
        else:
            end_line = self.get_line(try_jmp.end)
            if end_line:
                end_line.translated += "\n}\ncatch {}\n"
        self.jump_done(try_jmp)
        return True

    def handle_int_switch_case(self, swt):
        if "switch" not in swt.case_line:
            return

        start_line = self.get_line(swt.start)
        if not start_line:
            return

        # Begin the translation of the switch statement
        start_line.translated += f"\n{swt.case_line}\n{{\n"

        switch_end = set()
        case = self.jump_table[swt.type].get(swt.end)
        prev_start = swt.start

        while case:
            case_start_line = self.get_line(case.start)
            if case_start_line:
                case_start_line.translated = f'\n}}\n{case.case_line}\n{{\n' + case_start_line.translated

            # Check for existence of case break
            # Since we shifted jumps one step back will shift also case start which is the end of last case
            switch_end |= self.handle_break(prev_start, self.get_relative_offset(case.start, -1))

            prev_start = case.start
            self.jump_done(case)
            case = self.jump_table[case.type].get(case.end)

        if switch_end:
            switch_end |= self.handle_break(swt.last_case_start, self.get_relative_offset(min(switch_end), -1))

        switch_end = list(filter(lambda x: x > swt.last_case_start, sorted(switch_end)))

        # Handle switch with no end to last case, so close it immediately
        if not switch_end:
            last_case_line = self.get_line(swt.last_case_start)
            if last_case_line:
                last_case_line.translated = last_case_line.translated.replace("\n{\n", "\n{}\n")
            return True

        # Handle switch with no default case
        if len(switch_end) == 1:
            self.close_section(swt.last_case_start, switch_end[0])
            return True

        # Handle switch with default case
        if len(switch_end) == 2:
            first_end_line = self.get_line(switch_end[0])
            if first_end_line:
                first_end_line.translated += '\n}\ndefault:\n{\n'
            end = self.close_section(switch_end[0], switch_end[1])
            self.handle_break(switch_end[0], self.get_relative_offset(end, -1))
            return True

        # Default case when multiple ends
        penultimate_end_line = self.get_line(switch_end[-2])
        if penultimate_end_line:
            penultimate_end_line.translated += '\n}\ndefault:\n{\n'
        end = self.close_section(switch_end[-2], switch_end[-1])
        self.handle_break(switch_end[-2], end)

    def handle_int_switch(self, jmp):
        if jmp.type == "IntSwitch":
            self.handle_int_switch_case(jmp)
            return True

    def handle_switch(self, swt):
        # Ensure the jump type is 'If'
        if swt.type != "If":
            return False

        # Collect all 'If' jump cases within the switch jump's range
        cases = [c for c in self.jump_table['If'].values() if swt.start <= c.start <= swt.end <= c.end]
        if not cases:
            return False

        # Identify the default case as a 'Jump' right after the last case's start
        default_case = self.jump_table['Jump'].get(self.get_relative_offset(cases[-1].start, 1), None)

        # Validate that there are at least two distinct end values among the cases and that a default case exists
        if len(set(c.end for c in cases)) < 2 or not default_case or swt.end < default_case.start:
            return False

        # Append the default case and sort cases by their end value
        cases.append(default_case)
        cases.sort(key=lambda x: x.end)

        # Initialize switch-case statement and end marker
        case_line = "switch ()\n"
        switch_end = set()
        prev_case_start = None

        # Iterate over cases to build the switch-case structure
        for idx, case in enumerate(cases):
            if case == default_case:
                case_line += "default:\n"
            else:
                case_line += f"case CASE_{idx}:\n"
                case_start_line = self.get_line(case.start)
                if case_start_line:
                    case_start_line.translated = f"CASE_{idx} = ACCU"

            self.jump_done(case)

            if idx + 1 < len(cases) and case.end == cases[idx + 1].end:
                continue

            if prev_case_start:
                switch_end |= self.handle_break(self.get_relative_offset(prev_case_start, 1), case.end)

            case_end_line = self.get_line(case.end)
            if case_end_line:
                case_end_line.translated += (case_line + "{\n")
            prev_case_start = case.end
            case_line = "\n}\n"

        # Close the switch block at the end
        last_case_end_line = self.get_line(cases[-1].end)
        if not switch_end or (default_case and max(switch_end) == default_case.end):
            if last_case_end_line:
                last_case_end_line.translated = last_case_end_line.translated.replace("\n{\n", "\n{}\n")
            return True

        end = self.close_section(cases[-1].start, max(switch_end))
        self.handle_break(cases[-1].start, self.get_relative_offset(end, -1))
        return True

    def get_last_if_in_statement(self, jmp):
        # Initialize last_if with the provided jump
        last_if = jmp

        # Check for nested if statements
        while self.jump_table['If'].get(last_if.end, last_if) != last_if:
            if self.jump_table['If'].get(last_if.end, last_if).start == self.jump_table['If'].get(last_if.end, last_if).end:
                break
            last_if = self.jump_table['If'].get(last_if.end, last_if)

        # Check for multiple && if's by looking for an else jump
        else_jump = self.jump_table['Jump'].get(last_if.end, None)
        if else_jump:
            # Return the maximum (last) 'If' jump ending where the else jump starts
            last_if = max(
                (j for j in self.jump_table['If'].values() if j.end == else_jump.start),
                key=lambda x: x.start
            )

        # if last_if just skips a jump (far jump) change last_if.end to the end of the jump
        far_jump = self.jump_table['Jump'].get(self.get_relative_offset(last_if.start, 1), None)
        if far_jump:
            last_if.end = far_jump.end
            self.jump_done(far_jump)

        return last_if

    def invert_if_statement(self, statement):
        line = self.get_line(statement.start)
        if not line:
            return
        text = line.translated
        if " != " in text:
            line.translated = text.replace(" != ", " == ")
            return
        if "!" in text:
            line.translated = text.replace("!", "")
            return
        line.translated = text.replace("(", "(!")

    def get_or_and_table(self, all_if, last_if):
        known_type_table = {self.get_relative_offset(last_if.start, 1): "||", last_if.end: "&&"}
        known_types = [(self.get_relative_offset(last_if.start, 1), "||"), (last_if.end, "&&")]

        while known_types:
            known_start, known_type = known_types.pop(0)
            for jmp in all_if:
                if jmp.end == known_start and jmp.start not in known_type_table:
                    current_type = "||" if known_type == "&&" else "&&"
                    known_type_table[jmp.start] = current_type
                    known_types.append((jmp.start, current_type))
        return known_type_table

    def handle_if_statement(self, first_if):
        if first_if.type != "If":
            return False

        # If it's an if-jump that ends in the same line add {} (open and close) after the if end bracket (
        if first_if.start == first_if.end:
            first_line = self.get_line(first_if.start)
            if first_line:
                first_line.translated = first_line.translated.replace(")", ") {}\n")
            self.jump_done(first_if)
            return True

        last_if = self.get_last_if_in_statement(first_if)

        all_if = [i for i in self.jump_table['If'].values() if first_if.start <= i.start <= last_if.start and i.start != i.end]
        and_or_table = self.get_or_and_table(all_if, last_if)

        last_statement = "if"
        for if_jmp in all_if:
            if if_jmp.end not in and_or_table:
                continue

            if and_or_table[if_jmp.end] == "&&":
                self.invert_if_statement(if_jmp)

            if_line = self.get_line(if_jmp.start)
            if if_line:
                if_line.translated = if_line.translated.replace("if", last_statement)
            last_statement = "\t" + and_or_table[if_jmp.end]
            self.jump_done(if_jmp)

        last_if_line = self.get_line(last_if.start)
        if last_if_line:
            last_if_line.translated += "\n{"

        # Handle the else part if there is an else_jump
        else_jump = self.jump_table['Jump'].get(last_if.end, None)
        if else_jump and else_jump.start != else_jump.end:
            else_start = self.get_line(else_jump.start)
            if else_start:
                else_start.translated += "\n}\nelse\n{"
            end_num = self.close_section(else_jump.start, else_jump.end)
            self.jump_done(else_jump)
        else:
            self.close_section(last_if.start, last_if.end)

        return True

    def handle_if(self, jmp):
        if self.handle_switch(jmp):
            return
        self.handle_if_statement(jmp)

    def handle_jump(self, jmp):
        if jmp.start == jmp.end or self.get_relative_offset(jmp.start, 1) == jmp.end:
            return True
        # Unknown jump shape is ignored

    def remove_if_js_receiver(self):
        for jmp in list(self.jump_table["IfJSReceiver"].values()):
            # Normalize end to nearest existing offset to avoid infinite loop
            end_target = self.snap_to_existing_offset(jmp.end)
            if end_target is None:
                self.jump_done(jmp)
                continue

            idx = self.snap_to_existing_offset(jmp.start)
            if idx is None:
                self.jump_done(jmp)
                continue

            while idx != end_target:
                line = self.get_line(idx)
                if line:
                    line.translated = ""
                next_idx = self.get_relative_offset(idx, 1)
                if next_idx == idx:
                    break  # avoid infinite loop on degenerate data
                idx = next_idx

                # Mark Jumps in the current jump range as done
                for table in self.jump_table.values():
                    if idx in table:
                        self.jump_done(table[idx])

            last_line = self.get_line(end_target)
            if last_line:
                last_line.translated = ""
            self.jump_done(jmp)

        return True

    def expand_code_list(self):
        self.code_list.insert(0, CodeLine(translated="{"))
        i = 0
        while i < len(self.code_list):
            lines = self.code_list[i].translated.split('\n')
            if len(lines) > 1:
                self.code_list[i].translated = lines[0]
                for line in lines[1:]:
                    if not line:
                        continue
                    self.code_list.insert(i + 1, CodeLine(translated=line))
                    i += 1
            i += 1
        self.code_list.append(CodeLine(translated="}"))

    def convert(self):
        jump_type_handle = {"Loop": self.handle_loop,
                            "Exception": self.handle_exception,
                            "IntSwitch": self.handle_int_switch,
                            "If": self.handle_if,
                            "Jump": self.handle_jump}

        jump_list = self.get_all_jump_list()
        self.remove_if_js_receiver()

        for jmp in jump_list:
            if jmp.done:
                continue
            jump_type_handle.get(jmp.type, lambda x: None)(jmp)
        self.expand_code_list()


def convert_jumps_to_logical_flow(name, code, jump_table):
    JumpBlocks(name, code, jump_table).convert()