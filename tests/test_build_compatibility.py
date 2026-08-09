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


if __name__ == "__main__":
    unittest.main()
