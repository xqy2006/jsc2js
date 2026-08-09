import os
import unittest
from unittest import mock

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
        os.environ["PATH"] = original_path

    def test_modern_hooks_do_not_require_python_2(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            builder.activate_legacy_hook_python("9.4.146.8")


if __name__ == "__main__":
    unittest.main()
