#!/usr/bin/env python3
"""Apply the crash-safe jsc2js patch to modern V8 source trees.

V8 14.7 replaced the d8 file reader and completed the DirectHandle migration;
V8 14.9 then moved the generated object predicates.  A unified diff tied to
one checkout cannot safely span those changes.  This semantic patcher checks
the exact APIs before making five narrowly-scoped edits.

The source, version, flags, embedder-specific read-only snapshot identity, and
the embedder-sized part of the cache magic are relaxed for source-less caches
from another embedder.  Before normalizing its private in-memory magic copy,
the loader verifies the cache family and the exact header/payload boundary.
V8's remaining header, normalized magic, optional payload checksum, and
deserializer protocol checks still execute.  Printing the absent source text
is disabled so cached source positions cannot index the dummy text.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from patches.legacy.apply_legacy_patch import (  # noqa: E402
    PatchError,
    _ensure_include,
    _matching_brace,
    _remove_hash_mismatch_check,
    patch_string_printer,
    upstream_protections,
)


PATCH_MARKER = "JSC2JS_MODERN_PATCH"
MINIMUM_VERSION = "14.7.84"

D8_CC = "src/d8/d8.cc"
D8_H = "src/d8/d8.h"
BASE_MEMORY_H = "src/base/memory.h"
HANDLES_H = "src/handles/handles.h"
STRING_CC = "src/objects/string.cc"
PRINTER_CC = "src/diagnostics/objects-printer.cc"
SFI_H = "src/objects/shared-function-info.h"
BYTECODE_ARRAY_H = "src/objects/bytecode-array.h"
FIXED_ARRAY_H = "src/objects/fixed-array.h"
OBJECTS_H = "src/objects/objects.h"
OBJECTS_INL_H = "src/objects/objects-inl.h"
SERIALIZER_H = "src/snapshot/code-serializer.h"
SERIALIZER_CC = "src/snapshot/code-serializer.cc"
SNAPSHOT_DATA_H = "src/snapshot/snapshot-data.h"
DESERIALIZER_CC = "src/snapshot/deserializer.cc"
OBJECT_DESERIALIZER_CC = "src/snapshot/object-deserializer.cc"

SOURCE_PATHS = (
    D8_CC,
    D8_H,
    BASE_MEMORY_H,
    HANDLES_H,
    STRING_CC,
    PRINTER_CC,
    SFI_H,
    BYTECODE_ARRAY_H,
    FIXED_ARRAY_H,
    OBJECTS_H,
    OBJECTS_INL_H,
    SERIALIZER_H,
    SERIALIZER_CC,
    SNAPSHOT_DATA_H,
    DESERIALIZER_CC,
    OBJECT_DESERIALIZER_CC,
)


@dataclass(frozen=True)
class ModernFeatures:
    cache_type: str
    handle_type: str
    handle_container: str
    script_details: bool
    cached_script: bool
    read_chars_type: str
    object_predicate_generation: str
    bytecode_accessor: str
    constant_pool_type: str
    constant_pool_length_type: str
    sanity_style: str

    @property
    def family_name(self) -> str:
        return "-".join(
            (
                self.cache_type.lower(),
                self.handle_type.lower(),
                self.handle_container.lower(),
                self.read_chars_type.lower().replace("::", "-"),
                self.object_predicate_generation,
                self.bytecode_accessor,
                self.constant_pool_type.lower(),
                self.constant_pool_length_type.lower(),
                self.sanity_style,
            )
        )


def _read_existing(root: Path, paths: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in paths:
        path = root / relative
        if path.is_file():
            result[relative] = path.read_text(encoding="utf-8", errors="strict")
    return result


def _deserialize_signature(header: str) -> str:
    match = re.search(
        r"(?:V8_WARN_UNUSED_RESULT\s+)?static\s+"
        r"MaybeDirectHandle\s*<\s*SharedFunctionInfo\s*>\s*"
        r"Deserialize\s*\((.*?)\)\s*;",
        header,
        flags=re.DOTALL,
    )
    if not match:
        raise PatchError("modern CodeSerializer::Deserialize signature is missing")
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _trusted_fixed_array_length_type(fixed_array_h: str) -> str:
    """Classify the exact length() return used by TrustedFixedArray."""
    header = re.search(
        r"class ArrayHeaderBase<Super, true>.*?V8_OBJECT_END;",
        fixed_array_h,
        flags=re.DOTALL,
    )
    if header:
        body = header.group(0)
        if re.search(r"inline\s+int\s+length\(\)\s+const", body):
            return "int"
        if re.search(
            r"inline\s+SafeHeapObjectSize\s+length\(\)\s+const", body
        ):
            return "SafeHeapObjectSize"

    tagged_array = re.search(
        r"class TaggedArrayBase\s*:\s*public Super.*?"
        r"(?=V8_OBJECT class FixedArray)",
        fixed_array_h,
        flags=re.DOTALL,
    )
    if tagged_array and re.search(
        r"(?:inline\s+)?SafeHeapObjectSize\s+length\(\)\s+const",
        tagged_array.group(0),
    ):
        return "SafeHeapObjectSize"

    if "TrustedFixedArrayBase" in fixed_array_h and re.search(
        r"inline\s+SafeHeapObjectSize\s+length\(\)\s+const", fixed_array_h
    ):
        return "SafeHeapObjectSize"
    raise PatchError("modern TrustedFixedArray length API is unknown")


def detect_features(sources: dict[str, str]) -> ModernFeatures:
    missing = [path for path in SOURCE_PATHS if path not in sources]
    if missing:
        raise PatchError(f"modern V8 source files are missing: {', '.join(missing)}")

    d8 = sources[D8_CC]
    d8_h = sources[D8_H]
    base_memory_h = sources[BASE_MEMORY_H]
    handles_h = sources[HANDLES_H]
    serializer_h = sources[SERIALIZER_H]
    serializer_cc = sources[SERIALIZER_CC]
    snapshot_data_h = sources[SNAPSHOT_DATA_H]
    sfi_h = sources[SFI_H]
    bytecode_array_h = sources[BYTECODE_ARRAY_H]
    objects_h = sources[OBJECTS_H]
    objects_inl_h = sources[OBJECTS_INL_H]
    fixed_array_h = sources[FIXED_ARRAY_H]
    signature = _deserialize_signature(serializer_h)

    if '"src/snapshot/snapshot-data.h"' not in serializer_h:
        raise PatchError("CodeSerializer does not expose the SerializedData layout")
    magic_offset = re.search(
        r"kMagicNumberOffset\s*=\s*(?P<offset>\d+)\s*;", snapshot_data_h
    )
    if not magic_offset or int(magic_offset.group("offset")) != 0:
        raise PatchError("modern cache magic is not the first uint32_t header field")
    if not re.search(
        r"kMagicNumber\s*=\s*0xC0DE0000\s*\^\s*"
        r"ExternalReferenceTable::kSize\s*;",
        snapshot_data_h,
    ):
        raise PatchError("modern cache magic no longer uses ExternalReferenceTable::kSize")
    if not re.search(
        r"template\s*<\s*typename\s+V\s*>\s*"
        r"static\s+inline\s+V\s+ReadLittleEndianValue\s*\(\s*"
        r"Address\s+p\s*\)",
        base_memory_h,
    ):
        raise PatchError("modern little-endian Address read API is missing")
    if not re.search(
        r"template\s*<\s*typename\s+V\s*>\s*"
        r"static\s+inline\s+void\s+WriteLittleEndianValue\s*\(\s*"
        r"Address\s+p\s*,\s*V\s+value\s*\)",
        base_memory_h,
    ):
        raise PatchError("modern little-endian Address write API is missing")

    required_signature = (
        "AlignedCachedData",
        "DirectHandle<String>",
        "const ScriptDetails&",
    )
    absent = [token for token in required_signature if token not in signature]
    if absent:
        raise PatchError(
            "unsupported modern Deserialize signature; missing " + ", ".join(absent)
        )
    serialized_code_data = re.search(
        r"class\s+SerializedCodeData\s*:\s*public\s+SerializedData\s*"
        r"\{\s*public\s*:(?P<body>.*?)(?=\n\s*private\s*:)",
        serializer_h,
        flags=re.DOTALL,
    )
    if not serialized_code_data or not all(
        token in serialized_code_data.group("body")
        for token in ("kPayloadLengthOffset", "kHeaderSize")
    ):
        raise PatchError("SerializedCodeData preflight layout is not public")
    if not re.search(
        r"base::OwnedVector\s*<\s*char\s*>\s+Shell::ReadChars\s*\(", d8
    ):
        raise PatchError("unsupported modern d8 ReadChars implementation")
    if not re.search(
        r"static\s+base::OwnedVector\s*<\s*char\s*>\s+ReadChars\s*\(", d8_h
    ):
        raise PatchError("unsupported modern d8 ReadChars declaration")
    for token in (
        "class DirectHandleVector",
        "explicit DirectHandleVector(IsolateT* isolate)",
        "void push_back(const DirectHandle<T>& x)",
        "void pop_back()",
    ):
        if token not in handles_h:
            raise PatchError(f"modern DirectHandleVector API is missing: {token}")
    if "GetBytecodeArray(IsolateT* isolate)" not in sfi_h:
        raise PatchError("SharedFunctionInfo::GetBytecodeArray(IsolateT*) is missing")
    if "HasBytecodeArray" not in sfi_h:
        raise PatchError("SharedFunctionInfo::HasBytecodeArray is missing")
    if not re.search(
        r"constant_pool\s*,\s*TrustedFixedArray\s*\)", bytecode_array_h
    ):
        raise PatchError("modern TrustedFixedArray constant-pool API is missing")
    if "Tagged<ElementT> get(uint32_t index)" not in fixed_array_h:
        raise PatchError("modern tagged-array get API is missing")
    constant_pool_length_type = _trusted_fixed_array_length_type(fixed_array_h)

    if "IS_TYPE_FUNCTION_DECL" in objects_h and "Tagged<Object> obj" in objects_h:
        predicate_generation = "objects-h-macro"
    elif "IS_HELPER_DEF" in objects_inl_h and "CastTraits<Type>" in objects_inl_h:
        predicate_generation = "objects-inl-cast-traits"
    elif "DEF_CAST_TRAITS" in objects_inl_h:
        predicate_generation = "objects-inl-def-cast-traits"
    else:
        raise PatchError("modern IsSharedFunctionInfo predicate API is unknown")

    for token in (
        "SanityCheckWithoutSource",
        "SanityCheckJustSource",
        "kVersionHashOffset",
        "kFlagHashOffset",
        "kReadOnlySnapshotChecksumOffset",
        "kPayloadLengthOffset",
        "kChecksumOffset",
    ):
        if token not in serializer_cc:
            raise PatchError(f"modern serializer safety anchor is missing: {token}")

    return ModernFeatures(
        cache_type="AlignedCachedData",
        handle_type="DirectHandle",
        handle_container="DirectHandleVector",
        script_details=True,
        cached_script="maybe_cached_script" in signature,
        read_chars_type="base::OwnedVector<char>",
        object_predicate_generation=predicate_generation,
        bytecode_accessor="GetBytecodeArray(IsolateT*)",
        constant_pool_type="TrustedFixedArray",
        constant_pool_length_type=constant_pool_length_type,
        sanity_style="split-readonly-checksum",
    )


def _loadjsc_definition(
    constant_pool_length_type: str = "SafeHeapObjectSize",
) -> str:
    if constant_pool_length_type == "int":
        constant_count = "static_cast<uint32_t>(constants->length())"
    elif constant_pool_length_type == "SafeHeapObjectSize":
        constant_count = "constants->length().value()"
    else:
        raise PatchError(
            "unsupported TrustedFixedArray length type: "
            f"{constant_pool_length_type}"
        )
    return f"""

// {PATCH_MARKER}: crash-safe loader for source-less V8 code caches.
void Shell::LoadJSC(const FunctionCallbackInfo<Value>& args) {{
  i::Isolate* isolate =
      reinterpret_cast<i::Isolate*>(args.GetIsolate());
  i::HandleScope handle_scope(isolate);

  for (int file_index = 0; file_index < args.Length(); ++file_index) {{
    String::Utf8Value filename(args.GetIsolate(), args[file_index]);
    if (*filename == nullptr) {{
      args.GetIsolate()->ThrowException(Exception::Error(
          String::NewFromUtf8(args.GetIsolate(), "Invalid JSC filename",
                              NewStringType::kNormal)
              .ToLocalChecked()));
      return;
    }}

    base::OwnedVector<char> file_data = ReadChars(*filename);
    if (file_data.data() == nullptr || file_data.size() == 0) {{
      args.GetIsolate()->ThrowException(Exception::Error(
          String::NewFromUtf8(args.GetIsolate(), "Error reading JSC file",
                              NewStringType::kNormal)
              .ToLocalChecked()));
      return;
    }}

    // Validate everything needed to create a bounded payload view before
    // relaxing embedder-specific identity fields. V8's Payload() requires an
    // exact boundary even though its normal sanity check accepts trailing data.
    static_assert(i::SerializedData::kMagicNumberOffset == 0);
    constexpr uint32_t kEmbedderMagicBits = 0x0000FFFFu;
    static_assert(
        static_cast<uint32_t>(i::ExternalReferenceTable::kSize) <=
        kEmbedderMagicBits);
    if (file_data.size() < i::SerializedCodeData::kHeaderSize ||
        file_data.size() >
            static_cast<decltype(file_data.size())>(
                std::numeric_limits<int>::max())) {{
      args.GetIsolate()->ThrowException(Exception::Error(
          String::NewFromUtf8(args.GetIsolate(),
                              "Invalid JSC cache structure",
                              NewStringType::kNormal)
              .ToLocalChecked()));
      return;
    }}
    const i::Address cache_start =
        reinterpret_cast<i::Address>(file_data.data());
    const uint32_t original_magic =
        base::ReadLittleEndianValue<uint32_t>(
            cache_start + i::SerializedData::kMagicNumberOffset);
    const uint32_t payload_length =
        base::ReadLittleEndianValue<uint32_t>(
            cache_start + i::SerializedCodeData::kPayloadLengthOffset);
    const auto expected_payload_length =
        file_data.size() - i::SerializedCodeData::kHeaderSize;
    if ((original_magic & ~kEmbedderMagicBits) !=
            (i::SerializedData::kMagicNumber & ~kEmbedderMagicBits) ||
        payload_length != expected_payload_length) {{
      args.GetIsolate()->ThrowException(Exception::Error(
          String::NewFromUtf8(args.GetIsolate(),
                              "Invalid JSC cache structure",
                              NewStringType::kNormal)
              .ToLocalChecked()));
      return;
    }}

    // JSC2JS_EMBEDDER_MAGIC_NORMALIZATION: V8 folds the compile-time
    // ExternalReferenceTable size into this identity value. Electron and d8
    // can use different table sizes at the same V8 tag. Normalize only the
    // private file copy; the upstream magic checks still execute twice.
    base::WriteLittleEndianValue(cache_start,
                                 i::SerializedData::kMagicNumber);

    i::AlignedCachedData cached_data(
        reinterpret_cast<const uint8_t*>(file_data.data()),
        static_cast<int>(file_data.size()));
    i::DirectHandle<i::String> source =
        isolate->factory()
            ->NewStringFromUtf8(base::CStrVector("source"))
            .ToHandleChecked();
    i::ScriptDetails script_details;
    i::MaybeDirectHandle<i::SharedFunctionInfo> maybe_function =
        i::CodeSerializer::Deserialize(isolate, &cached_data, source,
                                       script_details);
    i::DirectHandle<i::SharedFunctionInfo> function;
    if (!maybe_function.ToHandle(&function)) {{
      args.GetIsolate()->ThrowException(Exception::Error(
          String::NewFromUtf8(
              args.GetIsolate(),
              "JSC deserialization failed (incompatible cache header or corrupt data)",
              NewStringType::kNormal)
              .ToLocalChecked()));
      return;
    }}

    // A flat worklist avoids recursive HeapObjectShortPrint expansion and the
    // stack-overflow cycle reported in issue #23.
    // DirectHandle cannot safely live in a normal std::vector. V8's dedicated
    // container registers its backing storage as strong roots across GC.
    i::DirectHandleVector<i::SharedFunctionInfo> pending(isolate);
    i::DirectHandleVector<i::SharedFunctionInfo> printed(isolate);
    pending.push_back(function);
    while (!pending.empty()) {{
      i::DirectHandle<i::SharedFunctionInfo> current = pending.back();
      pending.pop_back();
      bool seen = false;
      for (const auto& previous : printed) {{
        if (previous.is_identical_to(current)) {{
          seen = true;
          break;
        }}
      }}
      if (seen) continue;
      printed.push_back(current);
      if (!current->HasBytecodeArray()) continue;

      i::Tagged<i::BytecodeArray> bytecode =
          current->GetBytecodeArray(isolate);
      std::cout << "\\nStart SharedFunctionInfo\\n";
      current->SharedFunctionInfoPrint(std::cout);
      std::cout << "\\nStart BytecodeArray\\n";
      bytecode->Disassemble(std::cout);
      std::cout << "\\nEnd BytecodeArray\\n";
      std::cout << "End SharedFunctionInfo\\n" << std::flush;

      auto constants = bytecode->constant_pool();
      const uint32_t constant_count = {constant_count};
      for (uint32_t index = 0; index < constant_count; ++index) {{
        i::Tagged<i::Object> object = constants->get(index);
        if (i::IsSharedFunctionInfo(object)) {{
          pending.emplace_back(i::Cast<i::SharedFunctionInfo>(object), isolate);
        }}
      }}
    }}
  }}
}}
"""


def patch_d8_cc(text: str, features: ModernFeatures) -> str:
    if PATCH_MARKER in text:
        return text
    for include in (
        '"src/handles/handles.h"',
        '"src/snapshot/code-serializer.h"',
        "<iostream>",
        "<limits>",
    ):
        text = _ensure_include(text, include)

    read_chars = re.search(
        r"base::OwnedVector\s*<\s*char\s*>\s+Shell::ReadChars\s*\(", text
    )
    if not read_chars:
        raise PatchError("could not locate modern Shell::ReadChars")
    opening = text.find("{", read_chars.end())
    if opening < 0:
        raise PatchError("could not locate modern Shell::ReadChars body")
    closing = _matching_brace(text, opening)
    text = (
        text[: closing + 1]
        + _loadjsc_definition(features.constant_pool_length_type)
        + text[closing + 1 :]
    )

    create_global = text.find("Shell::CreateGlobalTemplate")
    if create_global < 0:
        raise PatchError("could not locate Shell::CreateGlobalTemplate")
    create_opening = text.find("{", create_global)
    create_closing = _matching_brace(text, create_opening)
    return_anchor = text.rfind(
        "  return global_template;", create_opening, create_closing
    )
    if return_anchor < 0:
        raise PatchError("could not locate CreateGlobalTemplate return")
    registration = f"""  // {PATCH_MARKER}: public d8 entry point.
  global_template->Set(isolate, "loadjsc",
                       FunctionTemplate::New(isolate, LoadJSC));
"""
    return text[:return_anchor] + registration + text[return_anchor:]


def patch_d8_h(text: str) -> str:
    if "static void LoadJSC(" in text:
        return text
    anchor = re.search(
        r"^(?P<indent>[ \t]*)static\s+base::OwnedVector\s*<\s*char\s*>\s+"
        r"ReadChars\s*\(",
        text,
        flags=re.MULTILINE,
    )
    if not anchor:
        raise PatchError("could not locate modern Shell::ReadChars declaration")
    declaration = (
        f"{anchor.group('indent')}static void LoadJSC("
        "const FunctionCallbackInfo<Value>& args);\n"
    )
    return text[: anchor.start()] + declaration + text[anchor.start() :]


def _bypass_read_only_snapshot_checksum(text: str) -> str:
    marker = "JSC2JS_READ_ONLY_SNAPSHOT_CHECKSUM_BYPASS"
    if marker in text:
        return text
    declaration = re.compile(
        r"(?m)^(?P<indent>[ \t]*)uint32_t\s+ro_snapshot_checksum\s*=\s*"
        r"(?:\r?\n[ \t]*)?GetHeaderValue\("
        r"kReadOnlySnapshotChecksumOffset\);[ \t]*$"
    )
    declarations = list(declaration.finditer(text))
    if len(declarations) != 1:
        raise PatchError(
            "expected one modern read-only snapshot checksum declaration, "
            f"found {len(declarations)}"
        )
    check = re.compile(
        r"(?m)^(?P<indent>[ \t]*)if\s*\(\s*ro_snapshot_checksum\s*!=\s*"
        r"expected_ro_snapshot_checksum\s*\)"
    )
    checks = list(check.finditer(text))
    if len(checks) != 1:
        raise PatchError(
            "expected one modern read-only snapshot checksum check, "
            f"found {len(checks)}"
        )
    found = checks[0]
    line_end = text.find("\n", found.end())
    if line_end < 0:
        line_end = len(text)
    opening = text.find("{", found.end(), line_end)
    if opening < 0:
        raise PatchError("read-only snapshot checksum check has no body")
    end = _matching_brace(text, opening) + 1
    original_check = text[found.start() : end]
    if not any(
        token in original_check
        for token in (
            "kReadOnlySnapshotChecksumMismatch",
            "READ_ONLY_SNAPSHOT_CHECKSUM_MISMATCH",
        )
    ):
        raise PatchError("unexpected read-only snapshot checksum mismatch result")
    text = (
        text[: found.start()]
        + found.group("indent")
        + f"// {marker}: accept matching V8 releases across embedder snapshots."
        + text[end:]
    )
    return declaration.sub(
        lambda match: (
            match.group("indent")
            + "static_cast<void>(expected_ro_snapshot_checksum);\n"
            + match.group("indent")
            + f"// {marker}: cached embedder snapshot identity ignored."
        ),
        text,
        count=1,
    )


def patch_serializer(text: str) -> str:
    if "JSC2JS_SOURCE_HASH_BYPASS" not in text:
        old = "return SanityCheckJustSource(expected_source_hash);"
        if text.count(old) != 1:
            raise PatchError(
                "expected one modern SanityCheckJustSource return, found "
                f"{text.count(old)}"
            )
        text = text.replace(
            old,
            "// JSC2JS_SOURCE_HASH_BYPASS: .jsc has no original source text.\n"
            "  return result;",
            1,
        )
    if "JSC2JS_VERSION_HASH_BYPASS" not in text:
        text = _remove_hash_mismatch_check(
            text,
            variable="version_hash",
            offset="kVersionHashOffset",
            condition=r"version_hash\s*!=\s*Version::Hash\(\)",
            result_tokens=("kVersionMismatch", "VERSION_MISMATCH"),
            marker="JSC2JS_VERSION_HASH_BYPASS",
        )
    if "JSC2JS_FLAGS_HASH_BYPASS" not in text:
        text = _remove_hash_mismatch_check(
            text,
            variable="flags_hash",
            offset="kFlagHashOffset",
            condition=r"flags_hash\s*!=\s*FlagList::Hash\(\)",
            result_tokens=("kFlagsMismatch", "FLAGS_MISMATCH"),
            marker="JSC2JS_FLAGS_HASH_BYPASS",
        )
    text = _bypass_read_only_snapshot_checksum(text)
    return text


def patch_sfi_printer(text: str) -> str:
    if "JSC2JS_SOURCE_PRINT_BYPASS" in text:
        return text
    declaration = text.find("void SharedFunctionInfo::SharedFunctionInfoPrint")
    if declaration < 0:
        raise PatchError("SharedFunctionInfoPrint function is missing")
    opening = text.find("{", declaration)
    closing = _matching_brace(text, opening)
    body = text[opening:closing]
    call = "  PrintSourceCode(os);"
    if body.count(call) != 1:
        raise PatchError(
            "expected one SharedFunctionInfoPrint source call, found "
            f"{body.count(call)}"
        )
    patched_body = body.replace(
        call,
        "  // JSC2JS_SOURCE_PRINT_BYPASS: source text is absent from .jsc.",
        1,
    )
    return text[:opening] + patched_body + text[closing:]


def transform_sources(
    sources: dict[str, str]
) -> tuple[dict[str, str], ModernFeatures, list[str]]:
    features = detect_features(sources)
    result = dict(sources)
    result[D8_CC] = patch_d8_cc(sources[D8_CC], features)
    result[D8_H] = patch_d8_h(sources[D8_H])
    result[SERIALIZER_CC] = patch_serializer(sources[SERIALIZER_CC])
    result[STRING_CC] = patch_string_printer(sources[STRING_CC])
    result[PRINTER_CC] = patch_sfi_printer(sources[PRINTER_CC])
    changed = sorted(path for path in result if result[path] != sources.get(path))
    expected = sorted((D8_CC, D8_H, PRINTER_CC, SERIALIZER_CC, STRING_CC))
    if changed != expected:
        raise PatchError(
            f"unexpected changed files: expected={expected} actual={changed}"
        )
    return result, features, changed


def load_tree(root: Path) -> dict[str, str]:
    return _read_existing(root, SOURCE_PATHS)


def apply_to_tree(root: Path, report_path: Path) -> dict:
    sources = load_tree(root)
    transformed, features, changed = transform_sources(sources)
    for relative in changed:
        (root / relative).write_text(
            transformed[relative], encoding="utf-8", newline="\n"
        )
    report = {
        "success": True,
        "root": str(root),
        "family": features.family_name,
        "features": asdict(features),
        "changed_files": changed,
        "safety": {
            "cross_embedder_identity_checks_bypassed": [
                "external_reference_table_size_magic",
                "source",
                "version",
                "flags",
                "read_only_snapshot",
            ],
            "loader_magic_normalized_to_local_table": True,
            "loader_requires_exact_header_payload_boundary": True,
            "loader_rejects_non_v8_magic_family": True,
            "upstream_magic_checks_preserved": True,
            "read_only_snapshot_checksum_preserved": False,
            "upstream_cache_checks_detected_before_patch": upstream_protections(
                sources[SERIALIZER_CC]
            ),
            "preserved_cache_checks": [
                "header_and_exact_payload_boundary",
                "v8_magic_family_before_normalization",
                "magic_after_loader_normalization",
                "payload_length",
                "payload_checksum",
            ],
            "deserializer_modified": False,
            "recursive_short_print_modified": False,
            "missing_source_print_disabled": True,
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--report", type=Path, default=Path("jsc2js_modern_patch_report.json")
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    report_path = args.report
    if not report_path.is_absolute():
        report_path = root / report_path
    try:
        report = apply_to_tree(root, report_path)
    except Exception as error:
        failure = {"success": False, "root": str(root), "error": str(error)}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(failure, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[modern-patch] ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
