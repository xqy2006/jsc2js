import unittest

from tools.audit_legacy_host_tools import (
    LEGACY_VCVARS_PATH_RE,
    classify_linux_host_mode,
    classify_object_print_gn_arg,
    extract_build_revision,
    normalize_template,
)


class HostToolAuditTest(unittest.TestCase):
    def test_extracts_build_revision_from_old_and_new_deps_keys(self):
        revision = "0123456789abcdef0123456789abcdef01234567"
        for key in ("v8/build", "build"):
            with self.subTest(key=key):
                deps = (
                    f"'{key}': Var('chromium_url') + "
                    f"'/chromium/src/build.git' + '@' + '{revision}',"
                )
                self.assertEqual(extract_build_revision(deps), revision)

    def test_normalizes_vcvars_argument_template(self):
        self.assertEqual(
            normalize_template("    args = [script_path, cpu_arg, ]"),
            "args = [script_path, cpu_arg, ]",
        )

    def test_classifies_the_two_hosted_clang_generations(self):
        self.assertEqual(
            classify_linux_host_mode("5.1.281.47", ""),
            "hosted-clang-in-tree-gyp",
        )
        self.assertEqual(
            classify_linux_host_mode("5.2.361.43", ""),
            "hosted-clang-without-sysroot-hook",
        )
        self.assertEqual(
            classify_linux_host_mode("5.3.332.37", "install-sysroot.py"),
            "pinned-clang-with-sysroot-hook",
        )

    def test_detects_only_the_legacy_vcvars_entry_point(self):
        for source in (
            "script_path = os.path.join(vs_path, 'VC', 'vcvarsall.bat')",
            "script_path = os.path.join(vs_path, 'VC/vcvarsall.bat')",
        ):
            with self.subTest(source=source):
                self.assertIsNotNone(
                    LEGACY_VCVARS_PATH_RE.search(source)
                )
        self.assertIsNone(
            LEGACY_VCVARS_PATH_RE.search(
                "script_path = os.path.join(vs_path, 'VC', 'Auxiliary', "
                "'Build', 'vcvarsall.bat')"
            )
        )

    def test_classifies_both_object_print_gn_argument_names(self):
        self.assertEqual(
            classify_object_print_gn_arg('v8_object_print = ""\n'),
            "v8_object_print",
        )
        self.assertEqual(
            classify_object_print_gn_arg("", "v8_enable_object_print = false\n"),
            "v8_enable_object_print",
        )
        self.assertEqual(classify_object_print_gn_arg("other = false\n"), "")


if __name__ == "__main__":
    unittest.main()
