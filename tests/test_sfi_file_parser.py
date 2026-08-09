import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "View8"))

from Parser.sfi_file_parser import parse_file  # noqa: E402


class FlatSharedFunctionInfoParserTest(unittest.TestCase):
    def test_links_flat_nested_function_by_address(self):
        dump = """\
Start SharedFunctionInfo
0x1000: [SharedFunctionInfo] in OldSpace
Parameter count 1
Register count 0
Constant pool (size = 1)
0: 0x2000 <SharedFunctionInfo child>
Handler Table (size = 0)
End SharedFunctionInfo
Start SharedFunctionInfo
0x2000: [SharedFunctionInfo] in OldSpace
Parameter count 1
Register count 0
Constant pool (size = 0)
Handler Table (size = 0)
End SharedFunctionInfo
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dump.txt"
            path.write_text(dump, encoding="utf-8")
            functions = parse_file(str(path))

        self.assertEqual(
            {"func_sfi_1000", "func_sfi_2000"}, set(functions)
        )
        self.assertEqual(
            ["func_sfi_2000"], functions["func_sfi_1000"].const_pool
        )
        self.assertEqual(
            "func_sfi_1000", functions["func_sfi_2000"].declarer
        )

    def test_parse_file_does_not_retain_previous_results(self):
        first = """\
Start SharedFunctionInfo
0x3000: [SharedFunctionInfo]
End SharedFunctionInfo
"""
        second = """\
Start SharedFunctionInfo
0x4000: [SharedFunctionInfo]
End SharedFunctionInfo
"""
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            first_path = directory / "first.txt"
            second_path = directory / "second.txt"
            first_path.write_text(first, encoding="utf-8")
            second_path.write_text(second, encoding="utf-8")
            parse_file(str(first_path))
            functions = parse_file(str(second_path))

        self.assertEqual({"func_sfi_4000"}, set(functions))


if __name__ == "__main__":
    unittest.main()
