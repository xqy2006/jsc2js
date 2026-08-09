import unittest

from patches.legacy.apply_legacy_patch import (
    Features,
    PatchError,
    _type_traversal,
    fixed_array_object_style,
    object_type_predicate_style,
    patch_serializer,
    shared_function_info_bytecode_accessor,
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

    def test_accepts_both_audited_inline_source_hash_templates(self):
        features = Features(
            layout="flat-d8",
            cache_type="ScriptData",
            origin_options=False,
            cached_script=False,
            sanity_style="inline",
            object_style="raw-pointer",
            object_predicate_style="member",
            bytecode_accessor="get",
            utf8_value_needs_isolate=False,
            read_chars_needs_isolate=False,
            flags_style="FLAG_",
        )
        for expected in ("expected_source_hash", "SourceHash(source)"):
            with self.subTest(expected=expected):
                source = f"""\
  uint32_t version_hash = GetHeaderValue(kVersionHashOffset);
  uint32_t source_hash = GetHeaderValue(kSourceHashOffset);
  uint32_t flags_hash = GetHeaderValue(kFlagHashOffset);
  if (version_hash != Version::Hash()) return VERSION_MISMATCH;
  if (source_hash != {expected}) return SOURCE_MISMATCH;
  if (flags_hash != FlagList::Hash()) return FLAGS_MISMATCH;
  if (!Checksum(Payload()).Check(c1, c2)) return CHECKSUM_MISMATCH;
"""
                patched = patch_serializer(source, features)
                self.assertIn("JSC2JS_SOURCE_HASH_BYPASS", patched)
                self.assertIn("JSC2JS_VERSION_HASH_BYPASS", patched)
                self.assertIn("JSC2JS_FLAGS_HASH_BYPASS", patched)
                self.assertIn("CHECKSUM_MISMATCH", patched)

    def test_classifies_the_v11_7_free_object_predicate_boundary(self):
        self.assertEqual(
            object_type_predicate_style(
                "#define IS_TYPE_FUNCTION_DECL(Type) "
                "V8_INLINE bool Is##Type(Tagged<Object> obj);"
            ),
            "free",
        )
        self.assertEqual(
            object_type_predicate_style(
                "#define IS_TYPE_FUNCTION_DECL(Type) "
                "V8_INLINE bool Is##Type() const;"
            ),
            "member",
        )

    def test_does_not_use_the_object_verifier_as_a_predicate_proxy(self):
        header = """
class Object { EXPORT_DECL_VERIFIER(Object) };
#define IS_TYPE_FUNCTION_DECL(Type) \\
  V8_INLINE bool Is##Type(Tagged<Object> obj);
"""
        self.assertEqual(object_type_predicate_style(header), "free")

    def test_value_object_uses_free_predicate_with_static_object_api(self):
        features = Features(
            layout="split-d8",
            cache_type="AlignedCachedData",
            origin_options=True,
            cached_script=True,
            sanity_style="split-readonly-checksum",
            object_style="value",
            object_predicate_style="free",
            bytecode_accessor="get-isolate",
            utf8_value_needs_isolate=True,
            read_chars_needs_isolate=True,
            flags_style="v8-flags",
        )
        traversal = _type_traversal(features)
        self.assertIn("i::IsSharedFunctionInfo(object)", traversal)
        self.assertNotIn("object.IsSharedFunctionInfo()", traversal)

    def test_scopes_old_bytecode_accessor_to_shared_function_info(self):
        header = """
class AbstractCode {
 public:
  inline BytecodeArray* GetBytecodeArray();
};
class SharedFunctionInfo: public HeapObject {
 public:
  inline bool HasBytecodeArray();
  inline BytecodeArray* bytecode_array();
};
"""
        self.assertEqual(
            shared_function_info_bytecode_accessor(header), "field"
        )


if __name__ == "__main__":
    unittest.main()
