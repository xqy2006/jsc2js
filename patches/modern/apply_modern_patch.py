#!/usr/bin/env python3
"""Apply the crash-safe jsc2js patch to modern V8 source trees.

V8 14.7 replaced the d8 file reader and completed the DirectHandle migration;
V8 14.9 then moved the generated object predicates.  A unified diff tied to
one checkout cannot safely span those changes.  This semantic patcher checks
the exact APIs before making four narrowly-scoped edits.

Only the source, version, and flags hashes are relaxed for source-less caches
from another embedder.  V8's header, magic, read-only snapshot, payload length,
checksum, and deserializer protocol checks remain intact.
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
STRING_CC = "src/objects/string.cc"
PRINTER_CC = "src/diagnostics/objects-printer.cc"
SFI_H = "src/objects/shared-function-info.h"
FIXED_ARRAY_H = "src/objects/fixed-array.h"
OBJECTS_H = "src/objects/objects.h"
OBJECTS_INL_H = "src/objects/objects-inl.h"
SERIALIZER_H = "src/snapshot/code-serializer.h"
SERIALIZER_CC = "src/snapshot/code-serializer.cc"
DESERIALIZER_CC = "src/snapshot/deserializer.cc"
OBJECT_DESERIALIZER_CC = "src/snapshot/object-deserializer.cc"

SOURCE_PATHS = (
    D8_CC,
    D8_H,
    STRING_CC,
    PRINTER_CC,
    SFI_H,
    FIXED_ARRAY_H,
    OBJECTS_H,
    OBJECTS_INL_H,
    SERIALIZER_H,
    SERIALIZER_CC,
    DESERIALIZER_CC,
    OBJECT_DESERIALIZER_CC,
)


@dataclass(frozen=True)
class ModernFeatures:
    cache_type: str
    handle_type: str
    script_details: bool
    cached_script: bool
    read_chars_type: str
    object_predicate_generation: str
    bytecode_accessor: str
    sanity_style: str

    @property
    def family_name(self) -> str:
        return "-".join(
            (
                self.cache_type.lower(),
                self.handle_type.lower(),
                self.read_chars_type.lower().replace("::", "-"),
                self.object_predicate_generation,
                self.bytecode_accessor,
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


def detect_features(sources: dict[str, str]) -> ModernFeatures:
    missing = [path for path in SOURCE_PATHS if path not in sources]
    if missing:
        raise PatchError(f"modern V8 source files are missing: {', '.join(missing)}")

    d8 = sources[D8_CC]
    d8_h = sources[D8_H]
    serializer_h = sources[SERIALIZER_H]
    serializer_cc = sources[SERIALIZER_CC]
    sfi_h = sources[SFI_H]
    objects_h = sources[OBJECTS_H]
    objects_inl_h = sources[OBJECTS_INL_H]
    fixed_array_h = sources[FIXED_ARRAY_H]
    signature = _deserialize_signature(serializer_h)

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
    if not re.search(
        r"base::OwnedVector\s*<\s*char\s*>\s+Shell::ReadChars\s*\(", d8
    ):
        raise PatchError("unsupported modern d8 ReadChars implementation")
    if not re.search(
        r"static\s+base::OwnedVector\s*<\s*char\s*>\s+ReadChars\s*\(", d8_h
    ):
        raise PatchError("unsupported modern d8 ReadChars declaration")
    if "GetBytecodeArray(IsolateT* isolate)" not in sfi_h:
        raise PatchError("SharedFunctionInfo::GetBytecodeArray(IsolateT*) is missing")
    if "HasBytecodeArray" not in sfi_h:
        raise PatchError("SharedFunctionInfo::HasBytecodeArray is missing")
    if "Tagged<ElementT> get(uint32_t index)" not in fixed_array_h:
        raise PatchError("modern tagged-array get API is missing")

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
        script_details=True,
        cached_script="maybe_cached_script" in signature,
        read_chars_type="base::OwnedVector<char>",
        object_predicate_generation=predicate_generation,
        bytecode_accessor="GetBytecodeArray(IsolateT*)",
        sanity_style="split-readonly-checksum",
    )


def _loadjsc_definition() -> str:
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
              "JSC deserialization failed (wrong V8 version, flags, or corrupt data)",
              NewStringType::kNormal)
              .ToLocalChecked()));
      return;
    }}

    // A flat worklist avoids recursive HeapObjectShortPrint expansion and the
    // stack-overflow cycle reported in issue #23.
    std::vector<i::DirectHandle<i::SharedFunctionInfo>> pending;
    std::vector<i::DirectHandle<i::SharedFunctionInfo>> printed;
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

      i::Tagged<i::FixedArray> constants = bytecode->constant_pool();
      for (int index = 0; index < constants->length(); ++index) {{
        i::Tagged<i::Object> object = constants->get(index);
        if (i::IsSharedFunctionInfo(object)) {{
          pending.emplace_back(i::Cast<i::SharedFunctionInfo>(object), isolate);
        }}
      }}
    }}
  }}
}}
"""


def patch_d8_cc(text: str) -> str:
    if PATCH_MARKER in text:
        return text
    for include in ('"src/snapshot/code-serializer.h"', "<iostream>", "<vector>"):
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
    text = text[: closing + 1] + _loadjsc_definition() + text[closing + 1 :]

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
    return text


def transform_sources(
    sources: dict[str, str]
) -> tuple[dict[str, str], ModernFeatures, list[str]]:
    features = detect_features(sources)
    result = dict(sources)
    result[D8_CC] = patch_d8_cc(sources[D8_CC])
    result[D8_H] = patch_d8_h(sources[D8_H])
    result[SERIALIZER_CC] = patch_serializer(sources[SERIALIZER_CC])
    result[STRING_CC] = patch_string_printer(sources[STRING_CC])
    changed = sorted(path for path in result if result[path] != sources.get(path))
    expected = sorted((D8_CC, D8_H, SERIALIZER_CC, STRING_CC))
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
            "source_version_flags_hashes_bypassed": True,
            "read_only_snapshot_checksum_preserved": True,
            "upstream_cache_checks_present": upstream_protections(
                sources[SERIALIZER_CC]
            ),
            "deserializer_modified": False,
            "recursive_short_print_modified": False,
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
