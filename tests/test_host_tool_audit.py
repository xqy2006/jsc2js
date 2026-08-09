import unittest

from tools.audit_legacy_host_tools import (
    LEGACY_VCVARS_PATH_RE,
    classify_linux_host_mode,
    classify_object_print_gn_arg,
    clang_hook_uses_keyed_dia_dll,
    extract_clang_revision,
    extract_dia_dll_years,
    extract_build_revision,
    extract_vs_toolchain_years,
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

    def test_extracts_clang_hook_revision(self):
        revision = "fedcba9876543210fedcba9876543210fedcba98"
        deps = (
            "'v8/tools/clang': Var('chromium_url') + "
            f"'/chromium/src/tools/clang.git' + '@' + '{revision}',"
        )
        self.assertEqual(extract_clang_revision(deps), revision)

    def test_extracts_visual_studio_years_from_exact_host_scripts(self):
        vs_toolchain = """
        SUPPORTED = [
          ('2019', '16.0'),
          ('2022', '17.0'),
        ]
        MSVC_TOOLSET_VERSION = {'2019': 'VC142', '2022': 'VC143'}
        """
        self.assertEqual(extract_vs_toolchain_years(vs_toolchain), ["2019", "2022"])

        clang_update = """
        DIA_DLL = {
          '2015': 'msdia140.dll',
          '2017': 'msdia140.dll',
        }
        dia_dll = DIA_DLL[msvs_version]
        """
        self.assertEqual(extract_dia_dll_years(clang_update), ["2015", "2017"])
        self.assertTrue(clang_hook_uses_keyed_dia_dll(clang_update))
        self.assertFalse(clang_hook_uses_keyed_dia_dll("dia_dll = GetDiaPath()"))

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
            "hosted-clang-lld-without-sysroot-hook",
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
