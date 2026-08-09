import unittest

from patches.legacy.apply_legacy_patch import (
    PatchError,
    fixed_array_object_style,
)


class FixedArrayObjectStyleTest(unittest.TestCase):
    def test_classifies_each_historical_get_return_type(self):
        cases = {
            "inline Object* get(int index) const;": "raw-pointer",
            "inline Object get(int index) const;": "value",
            "inline Tagged<Object> get(int index) const;": "tagged",
            "inline Tagged < Object > get(int index) const;": "tagged",
        }
        for declaration, expected in cases.items():
            with self.subTest(declaration=declaration):
                self.assertEqual(fixed_array_object_style(declaration), expected)

    def test_rejects_an_unknown_return_type(self):
        with self.assertRaises(PatchError):
            fixed_array_object_style("inline MaybeObject get(int index) const;")


if __name__ == "__main__":
    unittest.main()
