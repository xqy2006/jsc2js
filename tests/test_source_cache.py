from pathlib import Path
import unittest
from unittest import mock

from tools.audit_legacy_v8 import RawSourceCache


class RawSourceCacheTest(unittest.TestCase):
    def test_retries_the_transient_windows_atomic_replace_window(self):
        with (
            mock.patch.object(
                Path,
                "read_text",
                side_effect=[PermissionError("replace in progress"), "complete"],
            ) as read_text,
            mock.patch("tools.audit_legacy_v8.time.sleep") as sleep,
        ):
            self.assertEqual(
                RawSourceCache._read_published(Path("cached-source.txt")),
                "complete",
            )

        self.assertEqual(read_text.call_count, 2)
        sleep.assert_called_once_with(0.01)


if __name__ == "__main__":
    unittest.main()
