import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import build_versions_batch_v3 as builder
from patches.modern.apply_modern_patch import (
    _loadjsc_definition,
    patch_deserializer,
    patch_object_deserializer,
    patch_serializer,
    patch_sfi_printer,
)
from tools.update_failed_versions import update_failed_versions


class ModernPatchRoutingTest(unittest.TestCase):
    def test_exact_patch_boundaries(self):
        cases = {
            "11.9.169": (
                "legacy-semantic",
                "patches/legacy/apply_legacy_patch.py",
            ),
            "12.0.267.8": (
                "unified-diff",
                "patches/current/v8-12.0-to-12.5.patch",
            ),
            "12.6.228": (
                "unified-diff",
                "patches/current/v8-12.6-to-13.2.134.patch",
            ),
            "13.2.134": (
                "unified-diff",
                "patches/current/v8-12.6-to-13.2.134.patch",
            ),
            "13.2.135": (
                "unified-diff",
                "patches/current/v8-13.2.135-to-14.7.83.patch",
            ),
            "14.7.83": (
                "unified-diff",
                "patches/current/v8-13.2.135-to-14.7.83.patch",
            ),
            "14.7.84": (
                "modern-semantic",
                "patches/modern/apply_modern_patch.py",
            ),
            "15.3.25": (
                "modern-semantic",
                "patches/modern/apply_modern_patch.py",
            ),
        }
        for version, expected in cases.items():
            with self.subTest(version=version):
                self.assertEqual(builder.select_patch_implementation(version), expected)

    def test_rejects_non_exact_tags(self):
        for version in ("14.7", "v14.7.84", "14.7.84-beta"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                builder.select_patch_implementation(version)

    def test_resolves_only_the_matching_cache_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture.jsc"
            mapping = root / "mapping.json"
            mapping.write_text(
                json.dumps({"15.0.1240245": str(fixture)}), encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ,
                {"JSC2JS_VALID_CACHE_MAP_FILE": str(mapping)},
                clear=True,
            ):
                self.assertEqual(
                    builder.valid_cache_for_version("15.0.1240245"),
                    fixture.resolve(),
                )
                self.assertIsNone(builder.valid_cache_for_version("14.9.205"))

    def test_electron_fixtures_assert_the_exact_runtime_v8(self):
        root = Path(__file__).resolve().parents[1]
        generator = (root / "tests/generate_electron_cache.cjs").read_text(
            encoding="utf-8"
        )
        workflow = (root / ".github/workflows/compile.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Electron V8 mismatch", generator)
        self.assertIn('"$cache_path" "10.8.168.25"', workflow)
        self.assertIn('"$cache_path" "$v8_version"', workflow)

    def test_cross_embedder_failure_reports_the_sanity_reason(self):
        source = Path(builder.__file__).read_text(encoding="utf-8")
        self.assertIn('"--profile-deserialization"', source)

    def test_rejection_fixtures_cover_structure_before_deserialization(self):
        fixtures = builder.rejection_cache_fixtures()
        self.assertEqual(set(fixtures), {"short", "magic-family", "payload-length"})
        self.assertLess(len(fixtures["short"]), 32)
        self.assertEqual(
            int.from_bytes(fixtures["magic-family"][20:24], "little"),
            len(fixtures["magic-family"]) - 32,
        )
        self.assertEqual(
            int.from_bytes(fixtures["payload-length"][:4], "little")
            & 0xFFFF0000,
            0xC0DE0000,
        )
        self.assertNotEqual(
            int.from_bytes(fixtures["payload-length"][20:24], "little"),
            len(fixtures["payload-length"]) - 32,
        )


class ModernPatchSafetyTest(unittest.TestCase):
    def test_disables_only_the_missing_source_print_call(self):
        source = """\
void SharedFunctionInfo::SharedFunctionInfoPrint(std::ostream& os) {
  PrintHeader(os, "SharedFunctionInfo");
  PrintSourceCode(os);
  os << "\\n";
}

void HeapObject::HeapObjectShortPrint(std::ostream& os) {
  PrintSourceCode(os);
}
"""
        patched = patch_sfi_printer(source)
        self.assertIn("JSC2JS_SOURCE_PRINT_BYPASS", patched)
        self.assertEqual(patched.count("PrintSourceCode(os);"), 1)
        self.assertIn("void HeapObject::HeapObjectShortPrint", patched)

    def test_serializer_carries_the_legacy_global_sanity_fallback(self):
        source = """\
SerializedCodeSanityCheckResult SerializedCodeData::SanityCheck(
    uint32_t expected_ro_snapshot_checksum,
    uint32_t expected_source_hash) const {
  SerializedCodeSanityCheckResult result =
      SanityCheckWithoutSource(expected_ro_snapshot_checksum);
  if (result != SerializedCodeSanityCheckResult::kSuccess) return result;
  return SanityCheckJustSource(expected_source_hash);
}

SerializedCodeSanityCheckResult SerializedCodeData::SanityCheckJustSource(
    uint32_t expected_source_hash) const {
  return SerializedCodeSanityCheckResult::kSuccess;
}

SerializedCodeSanityCheckResult SerializedCodeData::SanityCheckWithoutSource(
    uint32_t expected_ro_snapshot_checksum) const {
  if (size_ < kHeaderSize) {
    return SerializedCodeSanityCheckResult::kInvalidHeader;
  }
  uint32_t version_hash = GetHeaderValue(kVersionHashOffset);
  if (version_hash != Version::Hash()) {
    return SerializedCodeSanityCheckResult::kVersionMismatch;
  }
  uint32_t flags_hash = GetHeaderValue(kFlagHashOffset);
  if (flags_hash != FlagList::Hash()) {
    return SerializedCodeSanityCheckResult::kFlagsMismatch;
  }
  if (Checksum(ChecksummedContent()) != checksum) {
    return SerializedCodeSanityCheckResult::kChecksumMismatch;
  }
  return SerializedCodeSanityCheckResult::kSuccess;
}
"""
        patched = patch_serializer(source)
        for marker in (
            "JSC2JS_SANITY_CHECK_FALLBACK",
            "JSC2JS_SANITY_CHECK_WITHOUT_SOURCE_FALLBACK",
            "JSC2JS_SOURCE_HASH_BYPASS",
            "JSC2JS_VERSION_HASH_BYPASS",
            "JSC2JS_FLAGS_HASH_BYPASS",
            "JSC2JS_READ_ONLY_SNAPSHOT_CHECKSUM_BYPASS",
        ):
            self.assertIn(marker, patched)
        self.assertEqual(
            patched.count("return SerializedCodeSanityCheckResult::kSuccess;"),
            3,
        )
        self.assertNotIn("SanityCheckWithoutSource(expected_ro_snapshot_checksum)", patched)
        self.assertNotIn("kInvalidHeader", patched)
        self.assertNotIn("kVersionMismatch", patched)
        self.assertNotIn("kFlagsMismatch", patched)
        self.assertNotIn("kChecksumMismatch", patched)

    def test_loader_uses_flat_direct_handle_worklist(self):
        loader = _loadjsc_definition()
        self.assertIn("base::OwnedVector<char> file_data", loader)
        self.assertIn("i::SerializedCodeData::kHeaderSize", loader)
        self.assertIn(
            "i::SerializedCodeData::kPayloadLengthOffset", loader
        )
        self.assertIn("base::ReadLittleEndianValue<uint32_t>", loader)
        self.assertIn("payload_length != expected_payload_length", loader)
        self.assertIn("original_magic & ~kEmbedderMagicBits", loader)
        self.assertLess(
            loader.index("payload_length != expected_payload_length"),
            loader.index("JSC2JS_EMBEDDER_MAGIC_NORMALIZATION"),
        )
        self.assertIn("JSC2JS_EMBEDDER_MAGIC_NORMALIZATION", loader)
        self.assertIn(
            "i::SerializedData::kMagicNumberOffset == 0", loader
        )
        self.assertIn("i::SerializedData::kMagicNumber", loader)
        self.assertIn("base::WriteLittleEndianValue(", loader)
        self.assertIn("i::MaybeDirectHandle<i::SharedFunctionInfo>", loader)
        self.assertIn(
            "i::DirectHandleVector<i::SharedFunctionInfo> pending(isolate)", loader
        )
        self.assertIn(
            "i::DirectHandleVector<i::SharedFunctionInfo> printed(isolate)", loader
        )
        self.assertIn("previous.is_identical_to(current)", loader)
        self.assertIn("pending.emplace_back", loader)
        self.assertIn("auto constants = bytecode->constant_pool();", loader)
        self.assertIn("constants->length().value()", loader)
        self.assertNotIn("i::Tagged<i::FixedArray> constants", loader)
        self.assertNotIn("std::vector<i::DirectHandle", loader)
        self.assertNotIn("void Disassemble(", loader)
        self.assertNotIn("HeapObjectShortPrint(", loader)

    def test_loader_supports_the_pre_strong_alias_length_api(self):
        loader = _loadjsc_definition("int")
        self.assertIn(
            "static_cast<uint32_t>(constants->length())", loader
        )
        self.assertNotIn("constants->length().value()", loader)

    def test_deserializer_falls_back_per_invalid_user_code_reference(self):
        source = """\
template <typename IsolateT>
Deserializer<IsolateT>::Deserializer(IsolateT* isolate,
                                     base::Vector<const uint8_t> payload,
                                     uint32_t magic_number,
                                     bool deserializing_user_code,
                                     bool can_rehash)
    : deserializing_user_code_(deserializing_user_code) {
  CHECK_EQ(magic_number_, SerializedData::kMagicNumber);
}

template <typename IsolateT>
void Deserializer<IsolateT>::Synchronize(VisitorSynchronization::SyncTag tag) {
  static const uint8_t expected = kSynchronize;
  CHECK_EQ(expected, source_.Get());
}

template <typename IsolateT>
DirectHandle<HeapObject> Deserializer<IsolateT>::ReadObject() {
  DirectHandle<HeapObject> ret;
  CHECK_EQ(ReadSingleBytecodeData(
               source_.Get(), SlotAccessorForHandle<IsolateT>(&ret, isolate())),
           1);
  return ret;
}

template <typename IsolateT>
Handle<HeapObject> Deserializer<IsolateT>::ReadObject(SnapshotSpace space) {
  const int size_in_tagged = source_.GetUint30();
  const int size_in_bytes = size_in_tagged * kTaggedSize;
  DirectHandle<Map> map = Cast<Map>(ReadObject());
  AllocationType allocation = SpaceToAllocation(space);
  return Handle<HeapObject>();
}

template <typename IsolateT>
Handle<HeapObject> Deserializer<IsolateT>::ReadMetaMap(SnapshotSpace space) {
  const int size_in_tagged = source_.GetUint30();
  const int size_in_bytes = size_in_tagged * kTaggedSize;
  const InstanceType instance_type =
      static_cast<InstanceType>(source_.GetUint30());
  Tagged<HeapObject> raw_obj =
      Allocate(SpaceToAllocation(space), size_in_bytes, kTaggedAligned);
  return Handle<HeapObject>();
}

template <typename IsolateT>
template <typename SlotAccessor>
int Deserializer<IsolateT>::ReadRepeatedRoot(SlotAccessor slot_accessor,
                                             int repeat_count) {
  CHECK_LE(2, repeat_count);
  return repeat_count;
}

template <typename IsolateT>
void Deserializer<IsolateT>::ReadData(Handle<HeapObject> object,
                                      int start_slot_index,
                                      int end_slot_index) {
  int current = start_slot_index;
  CHECK_EQ(current, end_slot_index);
}

template <typename IsolateT>
void Deserializer<IsolateT>::ReadData(FullMaybeObjectSlot start,
                                      FullMaybeObjectSlot end) {
  FullMaybeObjectSlot current = start;
  CHECK_EQ(current, end);
}

template <typename IsolateT>
template <typename SlotAccessor>
int Deserializer<IsolateT>::ReadSingleBytecodeData(uint8_t data,
                                                   SlotAccessor slot_accessor) {
  switch (data) {
    case kSynchronize:
      UNREACHABLE();
  }
  UNREACHABLE();
}

template <typename IsolateT>
Handle<HeapObject> Deserializer<IsolateT>::GetBackReferencedObject(
    uint32_t index) {
  Handle<HeapObject> obj = back_refs_[index];
  return obj;
}

template <typename IsolateT>
template <typename SlotAccessor>
int Deserializer<IsolateT>::ReadReadOnlyHeapRef(uint8_t data,
                                                SlotAccessor slot_accessor) {
  uint32_t chunk_index = source_.GetUint30();
  uint32_t chunk_offset = source_.GetUint30();

  ReadOnlySpace* read_only_space = isolate()->heap()->read_only_space();
  ReadOnlyPage* page = read_only_space->pages()[chunk_index];
  Address address = page->OffsetToAddress(chunk_offset);
  Tagged<HeapObject> heap_object = HeapObject::FromAddress(address);

  if (v8_flags.trace_deserialization) {
    ShortPrint(heap_object);
  }
  return WriteHeapPointer(slot_accessor, heap_object,
                          GetAndResetNextReferenceDescriptor(),
                          SKIP_WRITE_BARRIER);
}
"""
        patched = patch_deserializer(source)
        for marker in (
            "JSC2JS_MAGIC_CHECK_FALLBACK",
            "JSC2JS_SYNCHRONIZE_FALLBACK",
            "JSC2JS_READ_OBJECT_FALLBACK",
            "JSC2JS_OBJECT_SIZE_FALLBACK",
            "JSC2JS_READONLY_ALLOCATION_FALLBACK",
            "JSC2JS_META_MAP_ALLOCATION_FALLBACK",
            "JSC2JS_REPEAT_ROOT_FALLBACK",
            "JSC2JS_READ_DATA_OBJECT_FALLBACK",
            "JSC2JS_READ_DATA_ROOT_FALLBACK",
            "JSC2JS_SYNCHRONIZE_BYTECODE_FALLBACK",
            "JSC2JS_UNKNOWN_BYTECODE_FALLBACK",
            "JSC2JS_BACKREF_FALLBACK",
            "JSC2JS_READ_ONLY_REF_FALLBACK",
        ):
            self.assertIn(marker, patched)
        self.assertIn("index >= back_refs_.size()", patched)
        self.assertIn("chunk_index >= pages.size()", patched)
        self.assertIn("chunk_offset >= page->size()", patched)
        self.assertIn("ReadOnlyRoots(isolate()).undefined_value()", patched)
        self.assertIn("object_map->map() != ReadOnlyRoots(isolate()).meta_map()", patched)
        self.assertIn("return WriteHeapPointer(slot_accessor, heap_object", patched)
        self.assertIn("if (deserializing_user_code())", patched)
        self.assertIn("} else {", patched)
        self.assertIn(
            "page = read_only_space->pages()[chunk_index];", patched
        )

    def test_object_deserializer_rehash_fallback_is_migrated(self):
        source = """\
MaybeDirectHandle<HeapObject> ObjectDeserializer::Deserialize() {
  Rehash();
  CommitPostProcessedObjects();
}

MaybeDirectHandle<HeapObject> OffThreadObjectDeserializer::Deserialize() {
  Rehash();
}
"""
        patched = patch_object_deserializer(source)
        self.assertEqual(patched.count("JSC2JS_REHASH_FALLBACK"), 2)
        self.assertNotIn("\n  Rehash();", patched)


class FailedVersionTrackingTest(unittest.TestCase):
    def test_crlf_duplicates_are_normalized_and_successes_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failed = root / "failed.json"
            additions = root / "new.txt"
            failed.write_text(
                json.dumps(["15.2.124.5", "15.2.124.5\r", "bad"]),
                encoding="utf-8",
            )
            additions.write_bytes(b"14.7.84\r\n15.2.124.5\r\n\r\n")
            result = update_failed_versions(
                failed, add_files=[additions], remove=["15.2.124.5"]
            )
            self.assertEqual(result, ["14.7.84"])
            self.assertEqual(json.loads(failed.read_text(encoding="utf-8")), result)


if __name__ == "__main__":
    unittest.main()
