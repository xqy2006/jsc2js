import hashlib
import os
import re
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import build_versions_batch_v3 as builder


REPO_ROOT = Path(__file__).resolve().parents[1]


class LegacyHookPythonTest(unittest.TestCase):
    def test_stable_v12_patch_blobs_are_locked(self):
        expected = {
            "v8-12.0-to-12.5.patch": "baa4e8f1cc4e5465fbe797a641b3e7e8c1ae1246",
            "v8-12.6-to-13.2.134.patch": "cddc24c96135bafd720bcb428023eea0dfb93262",
            "v8-13.2.135-to-14.7.83.patch": "3ec516d1cbf30e54e2f25879db21a7d4a429fca5",
        }
        for name, blob in expected.items():
            with self.subTest(name=name):
                data = (REPO_ROOT / "patches" / "current" / name).read_bytes()
                # Git stores these text patches with LF even when a Windows
                # checkout materializes them as CRLF.
                data = data.replace(b"\r\n", b"\n")
                actual = hashlib.sha1(
                    f"blob {len(data)}\0".encode("ascii") + data
                ).hexdigest()
                self.assertEqual(actual, blob)

    def test_production_workflow_uses_the_legacy_compatible_builder(self):
        for name in ("main.yml", "update_worker.yml"):
            with self.subTest(name=name):
                workflow = (REPO_ROOT / ".github/workflows" / name).read_text(
                    encoding="utf-8"
                )
                self.assertIn("timeout-minutes: 330", workflow)
                self.assertIn("os: [ubuntu-22.04, windows-2022]", workflow)
                self.assertIn("build_versions_batch_v3.py", workflow)
                self.assertIn("build-essential clang lld", workflow)
                self.assertIn("git config --global core.longpaths true", workflow)
                self.assertIn("tools/install_windows_sdk.ps1", workflow)
                self.assertNotIn("python3 build_versions_batch.py", workflow)
                self.assertNotIn("patches/archive/generation-", workflow)
        main_workflow = (REPO_ROOT / ".github/workflows/main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("MIN_VERSION: 5.1.0", main_workflow)
        compile_workflow = (REPO_ROOT / ".github/workflows/compile.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tools/install_windows_sdk.ps1", compile_workflow)
        sdk_installer = (REPO_ROOT / "tools/install_windows_sdk.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('"10.0.28000.0"', sdk_installer)
        self.assertIn("https://go.microsoft.com/fwlink/?linkid=2372508", sdk_installer)
        self.assertIn("Get-AuthenticodeSignature", sdk_installer)

    def test_workflows_propagate_builder_failures_and_keep_diagnostics(self):
        for name in ("compile.yml", "main.yml", "update_worker.yml"):
            with self.subTest(name=name):
                workflow = (REPO_ROOT / ".github/workflows" / name).read_text(
                    encoding="utf-8"
                )
                self.assertIn('"$JSC2JS_PYTHON3" build_versions_batch_v3.py', workflow)
                self.assertNotRegex(
                    workflow, r"build_versions_batch_v3\.py\s*\|\|\s*true"
                )
                self.assertIn("if: always()", workflow)
        for name in ("main.yml", "update_worker.yml"):
            with self.subTest(normalization=name):
                workflow = (REPO_ROOT / ".github/workflows" / name).read_text(
                    encoding="utf-8"
                )
                self.assertIn("tr -d '\\r'", workflow)

    def test_old_batch_entry_points_delegate_to_the_current_builder(self):
        for name in ("build_versions_batch.py", "build_loop.py"):
            with self.subTest(name=name):
                source = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertIn("from build_versions_batch_v3 import main", source)
                self.assertNotIn("patches/archive/", source)

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
            "8.0.426.8": ("14.16.27023", "14.16"),
            "8.1.307.20": ("14.16.27023", "14.16"),
            "8.2.308.0": ("14.29.30133", "14.29"),
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
        vs_toolchain = root / "v8/build/vs_toolchain.py"
        vs_toolchain.write_text(
            "MSVS_VERSIONS = {'2017': '15.0', '2019': '16.0'}\n\n"
            "def GetVisualStudioVersion():\n"
            "  \"\"\"Return the detected Visual Studio version.\"\"\"\n"
            "  raise RuntimeError('host version was not recognized')\n",
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
            expected_year = builder.windows_compatibility_year(version)
            self.assertEqual(os.environ["GYP_MSVS_VERSION"], expected_year)
            self.assertEqual(os.environ[f"vs{expected_year}_install"], str(vs_root))
        patched = setup.read_text(encoding="utf-8")
        self.assertIn("JSC2JS_LEGACY_VCVARS_VERSION", patched)
        self.assertIn("-vcvars_ver=", patched)
        self.assertIn("JSC2JS_INSTALLED_WINDOWS_SDK", patched)
        self.assertIn("JSC2JS_WINDOWS_SDK_VERSION", patched)
        self.assertIn("JSC2JS_OPTIONAL_ATLMFC", patched)
        self.assertNotIn("assert vc_lib_atlmfc_path", patched)
        patched_vs_toolchain = vs_toolchain.read_text(encoding="utf-8")
        self.assertIn("JSC2JS_HOSTED_VS_VERSION", patched_vs_toolchain)
        self.assertIn("GYP_MSVS_VERSION", patched_vs_toolchain)
        legacy_vcvars = vs_root / "VC/vcvarsall.bat"
        self.assertTrue(legacy_vcvars.is_file())
        self.assertIn("Auxiliary\\Build", legacy_vcvars.read_text())

    def test_multiline_atlmfc_assertion_is_removed_as_one_statement(self):
        original = """def load(cpu):
  args = [script_path, cpu_arg]
  variables = _LoadEnvFromBat(args)
  if not target_store:
    assert vc_lib_atlmfc_path, ("Microsoft.VisualStudio.Component.VC.ATLMFC " +
                                "is not found, check if it's installed.")
  return args
"""
        patched = builder.patch_windows_setup_toolchain_source(original)
        compile(patched, "setup_toolchain.py", "exec")
        self.assertNotIn("assert vc_lib_atlmfc_path", patched)
        self.assertNotIn("is not found, check if it's installed", patched)
        self.assertEqual(patched.count("JSC2JS_OPTIONAL_ATLMFC"), 1)
        self.assertIn("return args", patched)
        self.assertEqual(builder.patch_windows_setup_toolchain_source(patched), patched)

    def test_installed_sdk_stays_before_the_vcvars_toolset_switch(self):
        original = """def load():
  args = [script_path, cpu_arg, '10.0.14393.0']
  variables = _LoadEnvFromBat(args)
  return args
"""
        patched = builder.patch_windows_setup_toolchain_source(original)
        namespace = {
            "os": os,
            "script_path": "vcvarsall.bat",
            "cpu_arg": "amd64",
            "_LoadEnvFromBat": lambda args: args,
        }
        with mock.patch.dict(
            os.environ,
            {
                "JSC2JS_VCVARS_VERSION": "14.16",
                "JSC2JS_WINDOWS_SDK_VERSION": "10.0.26100.0",
            },
            clear=True,
        ):
            exec(patched, namespace)
            args = namespace["load"]()
        self.assertEqual(
            args,
            [
                "vcvarsall.bat",
                "amd64",
                "10.0.26100.0",
                "-vcvars_ver=14.16",
            ],
        )

    def test_missing_um_lib_environment_uses_the_checked_sdk_directory(self):
        original = """def load():
  args = [script_path, cpu_arg]
  variables = _LoadEnvFromBat(args)
  return args

def locate_um_lib():
  win_sdk_path = sdk_root
  target_cpu = 'x64'
  vc_lib_um_path = ''
  assert vc_lib_um_path
  return vc_lib_um_path
"""
        with tempfile.TemporaryDirectory() as directory:
            sdk_root = Path(directory)
            expected = sdk_root / "Lib/10.0.26100.0/um/x64"
            expected.mkdir(parents=True)
            (expected / "User32.Lib").touch()
            namespace = {
                "os": os,
                "sdk_root": str(sdk_root),
                "script_path": "vcvarsall.bat",
                "cpu_arg": "amd64",
                "_LoadEnvFromBat": lambda args: args,
            }
            patched = builder.patch_windows_setup_toolchain_source(original)
            with mock.patch.dict(
                os.environ,
                {
                    "JSC2JS_VCVARS_VERSION": "14.16",
                    "JSC2JS_WINDOWS_SDK_VERSION": "10.0.26100.0",
                },
                clear=True,
            ):
                exec(patched, namespace)
                actual = namespace["locate_um_lib"]()
            self.assertEqual(actual, str(expected.resolve()))

    def test_selected_vs_year_bridges_newer_host_detection(self):
        original = """MSVS_VERSIONS = {'2017': '15.0', '2019': '16.0'}

def GetVisualStudioVersion():
  \"\"\"Return the best detected Visual Studio version.\"\"\"
  raise RuntimeError('VS 2022 is not in the historical table')
"""
        patched = builder.patch_windows_vs_toolchain_source(original)
        namespace = {"os": os}
        with mock.patch.dict(
            os.environ, {"GYP_MSVS_VERSION": "2019"}, clear=True
        ):
            exec(patched, namespace)
            self.assertEqual(namespace["GetVisualStudioVersion"](), "2019")
        self.assertEqual(
            namespace["GetVisualStudioVersion"].__doc__,
            "Return the best detected Visual Studio version.",
        )
        self.assertEqual(patched.count("JSC2JS_HOSTED_VS_VERSION"), 1)
        self.assertEqual(builder.patch_windows_vs_toolchain_source(patched), patched)

    def test_existing_vs_year_environment_support_is_unchanged(self):
        original = """def GetVisualStudioVersion():
  return os.environ.get('GYP_MSVS_VERSION', '2019')
"""
        self.assertEqual(builder.patch_windows_vs_toolchain_source(original), original)

    def test_vs_year_bridge_preserves_a_body_without_a_docstring(self):
        original = """MSVS_VERSIONS = {'2019': '16.0'}

def GetVisualStudioVersion():
  raise RuntimeError('host generation is unsupported')
"""
        patched = builder.patch_windows_vs_toolchain_source(original)
        namespace = {"os": os}
        with mock.patch.dict(
            os.environ, {"GYP_MSVS_VERSION": "2019"}, clear=True
        ):
            exec(patched, namespace)
            self.assertEqual(namespace["GetVisualStudioVersion"](), "2019")
        self.assertIn(
            "  raise RuntimeError('host generation is unsupported')", patched
        )

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
            self.assertIn("use_lld = true", args)
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
            build_gn.write_text('declare_args() {\n  v8_object_print = ""\n}\n')
            self.assertEqual(
                builder.object_print_gn_arg(root), "v8_object_print = true\n"
            )
            build_gn.write_text("declare_args() {\n  other_arg = false\n}\n")
            self.assertEqual(
                builder.object_print_gn_arg(root),
                "v8_enable_object_print = true\n",
            )

    def test_windows_disables_warning_promotion_only_when_declared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiler_gn = root / "build/config/compiler/BUILD.gn"
            compiler_gn.parent.mkdir(parents=True)
            compiler_gn.write_text(
                "declare_args() {\n  treat_warnings_as_errors = true\n}\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                builder.platform, "system", return_value="Windows"
            ):
                self.assertEqual(
                    builder.windows_warning_policy_gn_arg(root),
                    "treat_warnings_as_errors = false\n",
                )
            compiler_gn.write_text("declare_args() {\n}\n", encoding="utf-8")
            compiler_gni = root / "build/config/compiler/compiler.gni"
            compiler_gni.write_text(
                "declare_args() {\n  treat_warnings_as_errors = true\n}\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                builder.platform, "system", return_value="Windows"
            ):
                self.assertEqual(
                    builder.windows_warning_policy_gn_arg(root),
                    "treat_warnings_as_errors = false\n",
                )
            compiler_gni.write_text("declare_args() {\n}\n", encoding="utf-8")
            with mock.patch.object(
                builder.platform, "system", return_value="Windows"
            ), self.assertRaisesRegex(RuntimeError, "does not declare"):
                builder.windows_warning_policy_gn_arg(root)

    def test_non_windows_keeps_the_upstream_warning_policy(self):
        with mock.patch.object(builder.platform, "system", return_value="Linux"):
            self.assertEqual(builder.windows_warning_policy_gn_arg(), "")

    def test_current_windows_v8_has_no_legacy_toolset(self):
        self.assertIsNone(builder.windows_legacy_toolset_spec("10.8.168.25"))

    def test_v8_8_toolset_boundary_follows_the_pinned_clang_release(self):
        expected = {
            "8.0.426.8": "v141",
            "8.1.307.20": "v141",
            "8.2.308.0": "v142",
            "9.0.257.24": "v142",
        }
        for version, toolset in expected.items():
            with self.subTest(version=version):
                self.assertEqual(builder.windows_legacy_toolset_spec(version)[1], toolset)

    def test_visual_studio_compatibility_year_boundaries(self):
        expected = {
            "5.8.283.38": "2015",
            "6.9.427.24": "2017",
            "7.1.302.31": "2017",
            "8.9.255.25": "2019",
            "9.4.146.8": "2019",
            "10.8.168.25": "2022",
        }
        for version, year in expected.items():
            with self.subTest(version=version):
                self.assertEqual(builder.windows_compatibility_year(version), year)

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

    def test_windows_checkouts_enable_git_long_path_support(self):
        with mock.patch.object(
            builder.platform, "system", return_value="Windows"
        ), mock.patch.object(builder, "run") as run:
            builder.configure_windows_git_checkout()
        run.assert_called_once_with(
            "git config --global core.longpaths true", check=True
        )

    def test_linux_checkouts_do_not_change_git_long_path_config(self):
        with mock.patch.object(
            builder.platform, "system", return_value="Linux"
        ), mock.patch.object(builder, "run") as run:
            builder.configure_windows_git_checkout()
        run.assert_not_called()

    def test_validation_workflows_enable_git_long_path_support(self):
        for name in ("compile.yml", "legacy-audit.yml"):
            with self.subTest(name=name):
                workflow = (REPO_ROOT / ".github/workflows" / name).read_text(
                    encoding="utf-8"
                )
                self.assertIn("git config --global core.longpaths true", workflow)

    def test_depot_tools_updates_are_disabled_only_after_bootstrap(self):
        for name in (
            "compile.yml",
            "legacy-audit.yml",
            "main.yml",
            "update_worker.yml",
        ):
            with self.subTest(name=name):
                workflow = (REPO_ROOT / ".github/workflows" / name).read_text(
                    encoding="utf-8"
                )
                self.assertNotIn('DEPOT_TOOLS_UPDATE: "0"', workflow)
                pins = [
                    match.start()
                    for match in re.finditer("DEPOT_TOOLS_UPDATE=0", workflow)
                ]
                self.assertEqual(len(pins), 1 if name == "legacy-audit.yml" else 2)
                for pin in pins:
                    self.assertGreater(
                        workflow.rfind("gclient --version", 0, pin),
                        workflow.rfind("git clone", 0, pin),
                    )

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
