import unittest

from tools.audit_legacy_host_tools import extract_build_revision, normalize_template


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


if __name__ == "__main__":
    unittest.main()
