from pathlib import Path
import unittest

from determine_versions import select_pending_versions


class ReleaseRetryQueueTest(unittest.TestCase):
    def test_explicit_retries_keep_priority_and_bypass_failure_exclusion(self):
        selected = select_pending_versions(
            {"10.8.168.25", "14.7.84", "14.7.142"},
            {"14.7.84"},
            {"14.7.84", "14.7.142"},
            {"14.7.84", "99.0.0"},
        )
        self.assertEqual(selected, ["14.7.84", "10.8.168.25"])

    def test_release_workflow_consumes_retry_only_after_recording_outcome(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("public/retry_needed.json", workflow)
        self.assertIn("--add-file _missing_release.txt", workflow)
        self.assertEqual(
            workflow.count(
                "--failed-json public/retry_needed.json --remove \"$v\""
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
