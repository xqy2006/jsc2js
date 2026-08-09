import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import build_versions_batch_v3 as builder


class LegacyHookPythonTest(unittest.TestCase):
    def test_v8_51_uses_in_tree_gyp_only(self):
        self.assertTrue(builder.uses_in_tree_gyp("5.1.281.47"))
        self.assertFalse(builder.uses_in_tree_gyp("5.2.361.43"))

    def test_in_tree_gyp_batch_state_is_set_and_cleared(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(builder.configure_in_tree_gyp("5.1.281.47"))
            self.assertEqual(os.environ["GYP_GENERATORS"], "ninja")
            self.assertIn("v8_object_print=1", os.environ["GYP_DEFINES"])
            self.assertIn("v8_use_external_startup_data=1", os.environ["GYP_DEFINES"])
            self.assertFalse(builder.configure_in_tree_gyp("5.2.361.43"))
            self.assertNotIn("GYP_GENERATORS", os.environ)
            self.assertNotIn("GYP_GENERATOR_FLAGS", os.environ)
            self.assertNotIn("GYP_DEFINES", os.environ)

    def test_v8_51_linux_gyp_uses_the_runner_clang(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            builder.platform, "system", return_value="Linux"
        ):
            self.assertTrue(builder.configure_in_tree_gyp("5.1.281.47"))
            self.assertIn("clang_dir=/usr", os.environ["GYP_DEFINES"])
            self.assertIn("linux_use_bundled_gold=0", os.environ["GYP_DEFINES"])
            self.assertIn("werror=", os.environ["GYP_DEFINES"])

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
        vcvars = vs_root / "VC/Auxiliary/Build/vcvarsall.bat"
        vcvars.parent.mkdir(parents=True)
        vcvars.touch()
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
        legacy_vcvars = vs_root / "VC/vcvarsall.bat"
        self.assertTrue(legacy_vcvars.is_file())
        self.assertIn("Auxiliary\\Build", legacy_vcvars.read_text())

    def test_v8_52_linux_uses_hosted_clang_without_wheezy_sysroot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "third_party/llvm-build/Release+Asserts/bin"
            bundled.mkdir(parents=True)
            (bundled / "clang").write_text("downloaded-clang")
            os.link(bundled / "clang", bundled / "clang++")
            with mock.patch.object(
                builder.platform, "system", return_value="Linux"
            ), mock.patch.object(
                builder.shutil,
                "which",
                side_effect=lambda name: f"/usr/bin/{name}",
            ):
                args = builder.configure_v8_52_linux_gn("5.2.361.43", root)
            self.assertIn("use_sysroot = false", args)
            self.assertIn("clang_use_chrome_plugins = false", args)
            self.assertIn("treat_warnings_as_errors = false", args)
            self.assertNotIn("use_gold", args)
            self.assertEqual(
                (bundled / "clang").read_text(),
                '#!/bin/sh\nexec /usr/bin/clang "$@"\n',
            )
            self.assertEqual(
                (bundled / "clang++").read_text(),
                '#!/bin/sh\nexec /usr/bin/clang++ "$@"\n',
            )

    def test_v8_53_keeps_its_downloaded_sysroot_and_clang(self):
        with mock.patch.object(builder.platform, "system", return_value="Linux"):
            self.assertEqual(
                builder.configure_v8_52_linux_gn("5.3.332.37"), ""
            )

    def test_selects_the_object_print_arg_declared_by_the_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_gn = root / "BUILD.gn"
            build_gn.write_text("declare_args() {\n  v8_object_print = false\n}\n")
            self.assertEqual(
                builder.object_print_gn_arg(root), "v8_object_print = true\n"
            )
            build_gn.write_text("declare_args() {\n  other_arg = false\n}\n")
            self.assertEqual(
                builder.object_print_gn_arg(root),
                "v8_enable_object_print = true\n",
            )

    def test_current_windows_v8_has_no_legacy_toolset(self):
        self.assertIsNone(builder.windows_legacy_toolset_spec("10.8.168.25"))

    def test_v8_51_vcvars_uses_default_installed_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            vs_root = Path(directory)
            (vs_root / "VC/Tools/MSVC/14.29.30133").mkdir(parents=True)
            vcvars = vs_root / "VC/Auxiliary/Build/vcvarsall.bat"
            vcvars.parent.mkdir(parents=True)
            vcvars.touch()
            completed = mock.Mock(
                returncode=0,
                stdout="PATH=C:\\toolchain\nWindowsSDKVersion=10.0.26100.0\\\n",
                stderr="",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "GYP_MSVS_OVERRIDE_PATH": str(vs_root),
                    "JSC2JS_WINDOWS_SDK_VERSION": "10.0.26100.0",
                    "JSC2JS_ACTIVE_VCVARS_SIGNATURE": "",
                },
                clear=False,
            ), mock.patch.object(
                builder.platform, "system", return_value="Windows"
            ), mock.patch.object(
                builder.subprocess, "run", return_value=completed
            ) as run:
                builder.activate_windows_vcvars("5.1.281.47")
                builder.activate_windows_vcvars("5.1.281.59")
            self.assertEqual(run.call_count, 1)
            command = run.call_args.args[0]
            self.assertIsInstance(command, str)
            self.assertIn("cmd.exe /d /s /c", command)
            self.assertIn(" x64 -vcvars_ver=14.29", command)
            self.assertNotIn("-winsdk", command)
            legacy_vcvars = vs_root / "VC/vcvarsall.bat"
            self.assertTrue(legacy_vcvars.is_file())
            self.assertIn("Auxiliary\\Build", legacy_vcvars.read_text())

    def test_batch_restore_includes_external_build_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            v8_root = Path(directory) / "v8"
            (v8_root / ".git").mkdir(parents=True)
            (v8_root / "build" / ".git").mkdir(parents=True)
            with mock.patch.object(builder.subprocess, "run") as run:
                run.return_value.returncode = 0
                builder.restore_version_worktrees(v8_root)
        restored = [call.args[0][2] for call in run.call_args_list]
        self.assertEqual(restored, [str(v8_root), str(v8_root / "build")])

    def test_failed_build_preserves_patch_report_and_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v8_root = root / "v8"
            v8_root.mkdir()
            (v8_root / "apply_patch_report.json").write_text(
                '{"success": true}\n', encoding="utf-8"
            )
            target = builder.collect_audit_records(
                root / "artifacts",
                "5.1.281.47",
                "Windows",
                "example failure",
                v8_root,
            )
            self.assertTrue((target / "apply_patch_report.json").is_file())
            self.assertEqual(
                (target / "build_error.txt").read_text().strip(),
                "example failure",
            )


if __name__ == "__main__":
    unittest.main()
