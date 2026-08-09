import unittest

from tools.legacy_ci import parse_run_url, plan_batches, select_dispatched_run


class LegacyCiPlanTest(unittest.TestCase):
    def test_batches_do_not_cross_major_family_or_gyp_boundary(self):
        records = [
            {"version": "5.1.1", "family": "a"},
            {"version": "5.1.2", "family": "a"},
            {"version": "5.2.1", "family": "a"},
            {"version": "5.3.1", "family": "b"},
            {"version": "6.0.1", "family": "b"},
        ]
        batches = plan_batches(records, batch_size=5)
        self.assertEqual(
            [batch["versions"] for batch in batches],
            [["5.1.1", "5.1.2"], ["5.2.1"], ["5.3.1"], ["6.0.1"]],
        )

    def test_batch_size_is_bounded(self):
        records = [
            {"version": f"11.0.{index}", "family": "a"} for index in range(12)
        ]
        batches = plan_batches(records, batch_size=5)
        self.assertEqual([len(batch["versions"]) for batch in batches], [5, 5, 2])

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


if __name__ == "__main__":
    unittest.main()
