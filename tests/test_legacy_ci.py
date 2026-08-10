import json
import unittest
from subprocess import CalledProcessError, CompletedProcess
from unittest import mock

from tools.legacy_ci import (
    dispatch_has_failed_job,
    parse_run_url,
    plan_batches,
    recent_workflow_runs,
    refreshable_dispatches,
    select_dispatched_run,
    update_summary,
    view_run,
)


class LegacyCiPlanTest(unittest.TestCase):
    def test_batches_do_not_cross_major_family_or_gyp_boundary(self):
        records = [
            {"version": "5.1.1", "family": "a"},
            {"version": "5.1.2", "family": "a"},
            {"version": "5.2.1", "family": "a"},
            {"version": "5.3.1", "family": "b"},
            {"version": "6.0.1", "family": "b"},
        ]
        batches = plan_batches(records, batch_size=3)
        self.assertEqual(
            [batch["versions"] for batch in batches],
            [["5.1.1", "5.1.2"], ["5.2.1"], ["5.3.1"], ["6.0.1"]],
        )

    def test_batch_size_is_bounded(self):
        records = [
            {"version": f"11.0.{index}", "family": "a"} for index in range(12)
        ]
        batches = plan_batches(records, batch_size=3)
        self.assertEqual(
            [len(batch["versions"]) for batch in batches], [3, 3, 3, 3]
        )

    def test_rejects_a_batch_that_can_overrun_the_actions_limit(self):
        records = [
            {"version": f"15.0.{index}", "family": "a"} for index in range(4)
        ]
        with self.assertRaises(ValueError):
            plan_batches(records, batch_size=4)

    def test_extracts_run_id(self):
        run_id, url = parse_run_url(
            "https://github.com/xqy2006/jsc2js/actions/runs/31300983444\n"
        )
        self.assertEqual(run_id, 31300983444)
        self.assertTrue(url.endswith(str(run_id)))

    def test_selects_only_new_matching_dispatch(self):
        runs = [
            {
                "databaseId": 10,
                "url": "https://example.test/10",
                "headSha": "abc",
                "displayTitle": "Legacy V8 5.1.1",
            },
            {
                "databaseId": 11,
                "url": "https://example.test/11",
                "headSha": "other",
                "displayTitle": "Legacy V8 5.1.1",
            },
            {
                "databaseId": 12,
                "url": "https://example.test/12",
                "headSha": "abc",
                "displayTitle": "Legacy V8 5.2.1",
            },
            {
                "databaseId": 13,
                "url": "https://example.test/13",
                "headSha": "abc",
                "displayTitle": "Legacy V8 5.1.1",
            },
        ]
        selected = select_dispatched_run(
            runs, before_ids={10}, head="abc", display_title="Legacy V8 5.1.1"
        )
        self.assertEqual(selected["databaseId"], 13)

    def test_refreshes_only_the_latest_incomplete_dispatch(self):
        completed = {"run_id": 1, "status": "completed"}
        superseded = {"run_id": 2, "status": "completed"}
        active = {"run_id": 3, "status": "in_progress"}
        queued = {"run_id": 4, "status": "queued"}
        manifest = {
            "batches": [
                {"dispatches": [completed]},
                {"dispatches": [superseded, active]},
                {"dispatches": [queued]},
                {"dispatches": []},
            ]
        }
        self.assertEqual(refreshable_dispatches(manifest), [active, queued])

    def test_surfaces_a_failed_job_before_the_workflow_finishes(self):
        record = {
            "status": "in_progress",
            "conclusion": "",
            "jobs": [
                {"status": "completed", "conclusion": "failure"},
                {"status": "in_progress", "conclusion": ""},
            ],
        }
        manifest = {
            "summary": {},
            "batches": [
                {"versions": ["8.0.426.8"], "dispatches": [record]},
            ],
        }
        self.assertTrue(dispatch_has_failed_job(record))
        update_summary(manifest)
        self.assertEqual(manifest["summary"]["failed"], 1)
        self.assertEqual(manifest["summary"]["active"], 1)
        self.assertEqual(manifest["summary"]["verified_versions"], 0)

    def test_run_view_retries_a_transient_github_failure(self):
        payload = {
            "status": "in_progress",
            "conclusion": "",
            "headSha": "abc",
            "jobs": [],
            "url": "https://example.test/42",
        }
        with (
            mock.patch(
                "tools.legacy_ci.subprocess.run",
                side_effect=[
                    CalledProcessError(1, ["gh", "run", "view"]),
                    CompletedProcess([], 0, stdout=json.dumps(payload)),
                ],
            ) as run,
            mock.patch("tools.legacy_ci.time.sleep") as sleep,
        ):
            record, result = view_run(
                "xqy2006/jsc2js",
                {"run_id": 42},
                attempts=2,
                retry_delay=0.25,
            )
        self.assertEqual(record["run_id"], 42)
        self.assertEqual(result, payload)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_run_list_retries_a_transient_github_failure(self):
        payload = [
            {
                "databaseId": 42,
                "url": "https://example.test/42",
                "headSha": "abc",
                "displayTitle": "Legacy V8 8.8.74",
            }
        ]
        with (
            mock.patch(
                "tools.legacy_ci.subprocess.run",
                side_effect=[
                    CalledProcessError(1, ["gh", "run", "list"]),
                    CompletedProcess([], 0, stdout=json.dumps(payload)),
                ],
            ) as run,
            mock.patch("tools.legacy_ci.time.sleep") as sleep,
        ):
            result = recent_workflow_runs(
                "xqy2006/jsc2js",
                "compile.yml",
                "v12-legacy-support",
                attempts=2,
                retry_delay=0.25,
            )
        self.assertEqual(result, payload)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(0.25)


if __name__ == "__main__":
    unittest.main()
