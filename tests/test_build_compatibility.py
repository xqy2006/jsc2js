import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import build_versions_batch_v3 as builder


class LegacyHookPythonTest(unittest.TestCase):
    def test_legacy_hooks_force_python_2_ahead_of_depot_tools(self):
        original_path = os.environ.get("PATH", "")
        with mock.patch.dict(
            os.environ,
            {"JSC2JS_PYTHON2_DIR": "/compat/python2", "PATH": "/depot_tools"},
            clear=False,
        ), mock.patch.object(builder.Path, "is_dir", return_value=True), mock.patch.object(
            builder.shutil, "which", return_value="/compat/python2/python"
        ) as which, mock.patch.object(
            builder.subprocess,
            "check_output",
            return_value="Python 2.7.18\n",
        ):
            builder.activate_legacy_hook_python("7.6.274")
            self.assertEqual(
                os.environ["PATH"],
                "/compat/python2" + os.pathsep + "/depot_tools",
            )
            which.assert_called_once_with("python")
            self.assertEqual(
                os.environ["JSC2JS_HOOK_PYTHON"], "/compat/python2/python"
            )
            self.assertEqual(os.environ["DEPOT_TOOLS_UPDATE"], "0")
        os.environ["PATH"] = original_path

    def test_modern_hooks_do_not_require_python_2(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(builder.activate_legacy_hook_python("9.4.146.8"))

    def test_modern_hooks_remove_python_2_left_by_previous_batch_version(self):
        python2 = os.path.normpath("/compat/python2")
        path = os.pathsep.join((python2, "/depot_tools", "/usr/bin"))
        with mock.patch.dict(
            os.environ,
            {
                "JSC2JS_PYTHON2_DIR": python2,
                "JSC2JS_HOOK_PYTHON": python2 + "/python",
                "PATH": path,
            },
            clear=True,
        ):
            self.assertIsNone(builder.activate_legacy_hook_python("9.1.269.19"))
            self.assertEqual(
                os.environ["PATH"], os.pathsep.join(("/depot_tools", "/usr/bin"))
            )
            self.assertNotIn("JSC2JS_HOOK_PYTHON", os.environ)

    def test_gclient_hook_dispatch_uses_absolute_legacy_interpreter(self):
        original = """class Hook:\n    def run(self):\n        cmd = list(self._action)\n        run(cmd)\n"""
        with tempfile.TemporaryDirectory() as directory:
            gclient = Path(directory) / "gclient"
            gclient_py = Path(directory) / "gclient.py"
            gclient.touch()
            gclient_py.write_text(original, encoding="utf-8")
            with mock.patch.object(
                builder.shutil, "which", return_value=str(gclient)
            ):
                builder.patch_gclient_hook_dispatch("/compat/python2/python")
                builder.patch_gclient_hook_dispatch("/compat/python2/python")
            patched = gclient_py.read_text(encoding="utf-8")
            self.assertEqual(patched.count("JSC2JS_LEGACY_HOOK_PYTHON"), 1)
            self.assertIn('cmd[0] == "python"', patched)
            self.assertIn('os.environ["PATH"] = hook_dir', patched)

    def test_old_windows_v8_selects_matching_installed_toolset(self):
        templates = (
            "args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64']",
            "args = [script_path, 'amd64_x86' if cpu == 'x86' else 'amd64',\n"
            "        '10.0.14393.0']",
            "args = [script_path, cpu_arg]",
            "args = [script_path, cpu_arg, ]",
        )
        versions = {
            "5.8.283.38": ("14.29.30133", "14.29"),
            "6.7.288.43": ("14.16.27023", "14.16"),
            "7.6.274": ("14.16.27023", "14.16"),
            "8.5.189": ("14.29.30133", "14.29"),
            "9.4.146.8": ("14.29.30133", "14.29"),
        }
        for version, (installed, expected_vcvars) in versions.items():
            for args_line in templates:
                with self.subTest(version=version, args_line=args_line), \
                        tempfile.TemporaryDirectory() as directory:
                    self._assert_toolset_selected(
                        directory, args_line, version, installed, expected_vcvars
                    )

    def _assert_toolset_selected(
        self, directory, args_line, version, installed, expected_vcvars
    ):
        root = Path(directory)
        vs_root = root / "vs"
        (vs_root / "VC/Tools/MSVC" / installed).mkdir(parents=True)
        setup = root / "v8/build/toolchain/win/setup_toolchain.py"
        setup.parent.mkdir(parents=True)
        setup.write_text(
            f"def load(cpu):\n  {args_line}\n"
            "  variables = _LoadEnvFromBat(args)\n"
            "  if desktop:\n    assert vc_lib_atlmfc_path\n"
            "  return args\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ, {"GYP_MSVS_OVERRIDE_PATH": str(vs_root)}, clear=False
        ), mock.patch.object(
            builder.platform, "system", return_value="Windows"
        ):
            builder.configure_windows_legacy_toolset(version, root / "v8")
            self.assertEqual(
                os.environ["JSC2JS_VCVARS_VERSION"], expected_vcvars
            )
            expected_year = (
                "2015" if version.startswith("5.") else
                "2017" if version.startswith("6.") else "2019"
            )
            self.assertEqual(os.environ["GYP_MSVS_VERSION"], expected_year)
            self.assertEqual(os.environ[f"vs{expected_year}_install"], str(vs_root))
        patched = setup.read_text(encoding="utf-8")
        self.assertIn("JSC2JS_LEGACY_VCVARS_VERSION", patched)
        self.assertIn("-vcvars_ver=", patched)
        self.assertIn("JSC2JS_INSTALLED_WINDOWS_SDK", patched)
        self.assertIn("JSC2JS_WINDOWS_SDK_VERSION", patched)
        self.assertIn("JSC2JS_OPTIONAL_ATLMFC", patched)
        self.assertNotIn("assert vc_lib_atlmfc_path", patched)

    def test_current_windows_v8_has_no_legacy_toolset(self):
        self.assertIsNone(builder.windows_legacy_toolset_spec("10.8.168.25"))


if __name__ == "__main__":
    unittest.main()
