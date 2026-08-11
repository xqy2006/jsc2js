import tempfile
import unittest
from pathlib import Path
from typing import Optional

from tools.reconstruct_release_artifacts import reconstruct_artifacts


class ReleaseArtifactReconstructionTest(unittest.TestCase):
    def create_artifact(
        self,
        downloaded: Path,
        directory_name: str,
        binary_name: str,
        report_name: Optional[str] = None,
    ) -> Path:
        artifact = downloaded / "matrix-job" / "artifacts" / directory_name
        artifact.mkdir(parents=True)
        (artifact / binary_name).write_bytes(b"binary")
        (artifact / "snapshot_blob.bin").write_bytes(b"snapshot")
        if report_name:
            (artifact / report_name).write_text("report\n", encoding="utf-8")
        return artifact

    def test_json_only_report_does_not_fail_reconstruction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloaded = root / "downloaded"
            stage = root / "stage"
            self.create_artifact(
                downloaded,
                "d8-14.7.84-Linux",
                "d8",
                "apply_patch_report.json",
            )

            reconstructed = reconstruct_artifacts(downloaded, stage)

            self.assertEqual(
                [path.name for path in reconstructed], ["d8-14.7.84-Linux"]
            )
            target = stage / "d8-14.7.84-Linux"
            self.assertEqual((target / "d8").read_bytes(), b"binary")
            self.assertEqual(
                (target / "snapshot_blob.bin").read_bytes(), b"snapshot"
            )
            self.assertTrue((target / "apply_patch_report.json").is_file())
            self.assertFalse((target / "apply_patch_report.txt").exists())

    def test_missing_optional_report_keeps_both_platforms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            downloaded = root / "downloaded"
            stage = root / "stage"
            self.create_artifact(downloaded, "d8-10.8.168.25-Linux", "d8")
            self.create_artifact(downloaded, "d8-10.8.168.25-Windows", "d8.exe")
            unrelated = downloaded / "other" / "d8"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_bytes(b"ignore me")

            reconstructed = reconstruct_artifacts(downloaded, stage)

            self.assertEqual(
                {path.name for path in reconstructed},
                {"d8-10.8.168.25-Linux", "d8-10.8.168.25-Windows"},
            )
            self.assertTrue((stage / "d8-10.8.168.25-Linux" / "d8").is_file())
            self.assertTrue(
                (stage / "d8-10.8.168.25-Windows" / "d8.exe").is_file()
            )

    def test_production_workflow_uses_the_tested_reconstructor(self):
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "main.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("tools/reconstruct_release_artifacts.py", workflow)
        self.assertNotIn(
            '[ -f "$rep_dir/apply_patch_report.txt" ] && cp', workflow
        )


if __name__ == "__main__":
    unittest.main()
