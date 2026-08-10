import unittest

from tools.classify_legacy_host import classify_versions, parse_versions


class HostRequirementTest(unittest.TestCase):
    def test_classifies_mixed_batches_from_builder_toolset_rules(self):
        self.assertEqual(
            classify_versions(["7.9.317.25", "8.0.76"]),
            {
                "python2": True,
                "v141": True,
                "v142": False,
                "vs_year": "2017",
            },
        )
        self.assertEqual(
            classify_versions(["8.1.307.20", "8.2.308.0"]),
            {
                "python2": True,
                "v141": True,
                "v142": True,
                "vs_year": "2019",
            },
        )
        self.assertEqual(
            classify_versions(["10.0.139", "11.9.169.4"]),
            {
                "python2": False,
                "v141": False,
                "v142": False,
                "vs_year": "2022",
            },
        )

    def test_rejects_unbounded_duplicate_or_malformed_batches(self):
        for raw in (
            "[]",
            '["7.0.276.28","7.0.276.28"]',
            '["not-a-version"]',
            '["1.0.0","2.0.0","3.0.0","4.0.0","5.0.0","6.0.0"]',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_versions(raw)

    def test_validation_workflow_caps_batches_at_three_tags(self):
        with self.assertRaises(ValueError):
            parse_versions(
                '["15.0.19","15.0.30","15.0.43","15.0.108"]',
                maximum=3,
            )


if __name__ == "__main__":
    unittest.main()
