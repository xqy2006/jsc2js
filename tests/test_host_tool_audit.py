import unittest

from tools.audit_legacy_host_tools import (
    LEGACY_VCVARS_PATH_RE,
    audit_setup_toolchain_patch,
    audit_vs_toolchain_patch,
    clang_supports_selected_toolset,
    classify_linux_host_mode,
    classify_object_print_gn_arg,
    clang_hook_uses_keyed_dia_dll,
    extract_clang_release_version,
    extract_clang_revision,
    extract_dia_dll_years,
    extract_build_revision,
    extract_vs_toolchain_years,
    normalize_template,
    supports_warning_policy_gn_arg,
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

    def test_clang_release_controls_the_v142_header_boundary(self):
        self.assertEqual(
            extract_clang_release_version("RELEASE_VERSION = '10.0.0'\n"),
            "10.0.0",
        )
        self.assertEqual(extract_clang_release_version("CLANG_REVISION = 'old'\n"), "")
        self.assertFalse(clang_supports_selected_toolset("v142", "10.0.0"))
        self.assertTrue(clang_supports_selected_toolset("v141", "10.0.0"))
        self.assertTrue(clang_supports_selected_toolset("v142", "11.0.0"))

    def test_replays_and_tokenizes_the_multiline_atlmfc_template(self):
        source = """def load(cpu):
  args = [script_path, cpu_arg]
  variables = _LoadEnvFromBat(args)
  if not target_store:
    assert vc_lib_atlmfc_path, ("missing ATL/MFC " +
                                "from the installation")
  vc_lib_um_path = ''
  assert vc_lib_um_path
  return args
"""
        result = audit_setup_toolchain_patch(source)
        self.assertTrue(result["supported"], result)
        self.assertTrue(result["checks"]["tokenizable"])
        self.assertTrue(result["checks"]["idempotent"])
        self.assertTrue(result["checks"]["sdk_precedes_vcvars_guard"])
        self.assertTrue(result["checks"]["um_lib_fallback_complete"])
        self.assertTrue(result["um_lib_fallback_anchor_present"])

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

    def test_replays_the_hosted_vs_version_bridge(self):
        source = """MSVS_VERSIONS = {
  '2017': '15.0',
  '2019': '16.0',
}

def GetVisualStudioVersion():
  \"\"\"Return the best detected version.\"\"\"
  raise RuntimeError('host generation is unsupported')
"""
        result = audit_vs_toolchain_patch(source, "2019")
        self.assertTrue(result["supported"], result)
        self.assertTrue(result["bridge_required"])
        self.assertTrue(result["checks"]["original_lines_preserved"])
        self.assertTrue(result["checks"]["tokenizable"])

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

    def test_detects_the_exact_warnings_as_errors_declaration(self):
        self.assertTrue(
            supports_warning_policy_gn_arg(
                "declare_args() {\n  treat_warnings_as_errors = true\n}\n"
            )
        )
        self.assertTrue(
            supports_warning_policy_gn_arg(
                "config(\"warnings\") {}\n",
                "declare_args() {\n  treat_warnings_as_errors = true\n}\n",
            )
        )
        self.assertFalse(
            supports_warning_policy_gn_arg("use_warnings_as_errors = true\n")
        )


if __name__ == "__main__":
    unittest.main()
