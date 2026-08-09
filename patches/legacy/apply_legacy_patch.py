#!/usr/bin/env python3
"""Apply the crash-safe jsc2js patch to V8 5.1 through 11.9 sources.

This is a semantic patcher rather than a fuzzy unified diff.  V8 moved d8 and
the object implementation several times in this range, so the patcher detects
the source API that is actually present and requires every edit anchor to match
exactly once.  It keeps the structural and payload-integrity checks while
allowing the version, source, and flags hashes that legitimately differ
between upstream d8 and an Electron build from the same V8 release line.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
from typing import Iterable


PATCH_MARKER = "JSC2JS_LEGACY_PATCH"

D8_CC_PATHS = ("src/d8/d8.cc", "src/d8.cc")
D8_H_PATHS = ("src/d8/d8.h", "src/d8.h")
PRINTER_PATHS = (
    "src/diagnostics/objects-printer.cc",
    "src/objects/objects-printer.cc",
    "src/objects-printer.cc",
)
HEAP_PATHS = (
    "src/diagnostics/objects-printer.cc",
    "src/objects/objects.cc",
    "src/objects.cc",
)
STRING_PATHS = ("src/objects/string.cc", "src/objects.cc")
SFI_PATHS = (
    "src/objects/shared-function-info.h",
    "src/objects/shared-function-info-inl.h",
    "src/objects.h",
)
FIXED_ARRAY_PATHS = (
    "src/objects/fixed-array.h",
    "src/objects.h",
)
OBJECTS_H_PATHS = (
    "src/objects/objects.h",
    "src/objects.h",
)
SERIALIZER_H = "src/snapshot/code-serializer.h"
SERIALIZER_CC = "src/snapshot/code-serializer.cc"

UPSTREAM_PROTECTION_TOKENS = {
    "magic": ("MAGIC_NUMBER_MISMATCH", "kMagicNumberMismatch"),
    "checksum": ("CHECKSUM_MISMATCH", "kChecksumMismatch"),
    "invalid_header": ("INVALID_HEADER", "kInvalidHeader"),
    "payload_length": ("LENGTH_MISMATCH", "kLengthMismatch"),
    "cpu_features": ("CPU_FEATURES_MISMATCH", "kCpuFeaturesMismatch"),
    "read_only_snapshot_checksum": (
        "READ_ONLY_SNAPSHOT_CHECKSUM_MISMATCH",
        "kReadOnlySnapshotChecksumMismatch",
    ),
}


class PatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Features:
    layout: str
    cache_type: str
    origin_options: bool
    cached_script: bool
    sanity_style: str
    object_style: str
    object_predicate_style: str
    bytecode_accessor: str
    utf8_value_needs_isolate: bool
    read_chars_needs_isolate: bool
    flags_style: str

    @property
    def family_name(self) -> str:
        origin = "origin" if self.origin_options else "no-origin"
        cached_script = "cached-script" if self.cached_script else "no-cached-script"
        utf8 = "utf8-isolate" if self.utf8_value_needs_isolate else "utf8-value"
        read_chars = (
            "read-isolate" if self.read_chars_needs_isolate else "read-filename"
        )
        return "-".join(
            (
                self.layout,
                self.cache_type.lower(),
                origin,
                cached_script,
                self.sanity_style,
                self.object_style,
                self.object_predicate_style,
                self.bytecode_accessor,
                utf8,
                read_chars,
                self.flags_style,
            )
        )


def _read_existing(root: Path, paths: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in paths:
        path = root / relative
        if path.is_file():
            result[relative] = path.read_text(encoding="utf-8", errors="strict")
    return result


def upstream_protections(serializer: str) -> dict[str, bool]:
    """Report the integrity checks that the selected upstream V8 provides."""
    return {
        name: any(token in serializer for token in tokens)
        for name, tokens in UPSTREAM_PROTECTION_TOKENS.items()
    }


def _select(
    sources: dict[str, str], paths: Iterable[str], marker: str
) -> tuple[str, str]:
    for path in paths:
        content = sources.get(path)
        if content is not None and marker in content:
            return path, content
    raise PatchError(f"no source containing {marker!r}: {', '.join(paths)}")


def _serializer_signature(header: str) -> str:
    match = re.search(
        r"(?:MUST_USE_RESULT|V8_WARN_UNUSED_RESULT)\s+static\s+"
        r"MaybeHandle<SharedFunctionInfo>\s+Deserialize\s*\((.*?)\)\s*;",
        header,
        flags=re.DOTALL,
    )
    if not match:
        raise PatchError("could not identify CodeSerializer::Deserialize signature")
    return re.sub(r"\s+", " ", match.group(1)).strip()


def fixed_array_object_style(header: str) -> str:
    """Classify the object API from the exact FixedArray::get return type."""
    if re.search(r"\bTagged\s*<\s*Object\s*>\s+get\s*\(", header):
        return "tagged"
    if re.search(r"\bObject\s+get\s*\(", header):
        return "value"
    if re.search(r"\bObject\s*\*\s*get\s*\(", header):
        return "raw-pointer"
    raise PatchError("unknown FixedArray::get return type")


def object_type_predicate_style(header: str) -> str:
    """Classify the generated Object predicate declarations themselves.

    V8 11.7.349 removed value-style calls such as
    ``object.IsSharedFunctionInfo()`` even though V8 11.7.300 still had them and
    the unrelated Object verifier macro did not become static until V8 11.8.
    Detect the generated predicate signature rather than using either the V8
    version or verifier declaration as a proxy.
    """
    if re.search(r"\bIs##(?:Type|type_?)\s*\(\s*\)\s*const\b", header):
        return "member"
    if re.search(
        r"\bIs##(?:Type|type_?)\s*\(\s*Tagged\s*<\s*Object\s*>\s+obj\b",
        header,
    ):
        return "free"
    raise PatchError("Object type-predicate declaration is missing")


def shared_function_info_bytecode_accessor(header: str) -> str:
    """Classify the accessor declared by SharedFunctionInfo itself."""
    declaration = re.search(
        r"\bclass\s+(?:V8_EXPORT_PRIVATE\s+)?SharedFunctionInfo\b[^;{]*\{",
        header,
    )
    if not declaration:
        raise PatchError("SharedFunctionInfo class declaration is missing")
    opening = header.find("{", declaration.start())
    closing = _matching_brace(header, opening)
    body = header[opening + 1 : closing]

    if re.search(r"\bbytecode_array\s*\(\s*\)", body):
        return "field"
    if re.search(
        r"\bGetBytecodeArray\s*\(\s*(?:template\s*<[^>]+>\s*)?"
        r"(?:Local)?Isolate(?:T)?\s*\*",
        body,
    ):
        return "get-isolate"
    if re.search(r"\bGetBytecodeArray\s*\(", body):
        return "get"
    raise PatchError("SharedFunctionInfo bytecode accessor is missing")


def detect_features(sources: dict[str, str]) -> Features:
    d8_path, d8 = _select(sources, D8_CC_PATHS, "Shell::CreateGlobalTemplate")
    _, printer = _select(
        sources, PRINTER_PATHS, "SharedFunctionInfo::SharedFunctionInfoPrint"
    )
    _, heap = _select(sources, HEAP_PATHS, "HeapObject::HeapObjectShortPrint")
    serializer_h = sources.get(SERIALIZER_H, "")
    serializer_cc = sources.get(SERIALIZER_CC, "")
    if not serializer_h or not serializer_cc:
        raise PatchError("code serializer sources are missing")
    signature = _serializer_signature(serializer_h)

    sfi_sources = "\n".join(sources.get(path, "") for path in SFI_PATHS)
    _, fixed_array = _select(sources, FIXED_ARRAY_PATHS, "FixedArray")
    _, objects_header = _select(sources, OBJECTS_H_PATHS, "class Object")
    bytecode_accessor = shared_function_info_bytecode_accessor(sfi_sources)

    if "AlignedCachedData" in signature:
        cache_type = "AlignedCachedData"
    elif "ScriptData" in signature:
        cache_type = "ScriptData"
    else:
        raise PatchError(f"unknown cached data type in: {signature}")

    if "SanityCheckWithoutSource" not in serializer_cc:
        sanity_style = "inline"
    elif "SanityCheckJustSource" not in serializer_cc:
        sanity_style = "split-inline-source"
    elif re.search(
        r"SanityCheckWithoutSource\s*\(\s*uint32_t", serializer_cc
    ):
        sanity_style = "split-readonly-checksum"
    else:
        sanity_style = "split"

    object_style = fixed_array_object_style(fixed_array)

    return Features(
        layout="split-d8" if d8_path == "src/d8/d8.cc" else "flat-d8",
        cache_type=cache_type,
        origin_options="ScriptOriginOptions" in signature,
        cached_script="maybe_cached_script" in signature,
        sanity_style=sanity_style,
        object_style=object_style,
        object_predicate_style=object_type_predicate_style(objects_header),
        bytecode_accessor=bytecode_accessor,
        utf8_value_needs_isolate=bool(
            re.search(r"String::Utf8Value\s+\w+\s*\(\s*isolate\s*,", d8)
        ),
        read_chars_needs_isolate=bool(
            re.search(
                r"static\s+char\s*\*\s*ReadChars\s*\(\s*Isolate\s*\*", d8
            )
        ),
        flags_style="v8-flags" if "v8_flags." in serializer_cc else "flag-globals",
    )


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    state = "code"
    quote = ""
    index = opening
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "line-comment":
            if char == "\n":
                state = "code"
        elif state == "block-comment":
            if char == "*" and nxt == "/":
                state = "code"
                index += 1
        elif state == "string":
            if char == "\\":
                index += 1
            elif char == quote:
                state = "code"
        else:
            if char == "/" and nxt == "/":
                state = "line-comment"
                index += 1
            elif char == "/" and nxt == "*":
                state = "block-comment"
                index += 1
            elif char in {'"', "'"}:
                state = "string"
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index
        index += 1
    raise PatchError("unterminated C++ function while locating patch anchor")


def _remove_hash_mismatch_check(
    text: str,
    *,
    variable: str,
    offset: str,
    condition: str,
    result_tokens: tuple[str, ...],
    marker: str,
) -> str:
    """Remove one hash declaration/check without touching adjacent safeguards."""
    declaration = re.compile(
        rf"(?m)^(?P<indent>[ \t]*)uint32_t\s+{variable}\s*=\s*"
        rf"GetHeaderValue\({offset}\);[ \t]*$"
    )
    declarations = list(declaration.finditer(text))
    if len(declarations) != 1:
        raise PatchError(
            f"expected one {variable} declaration, found {len(declarations)}"
        )

    check = re.compile(
        rf"(?m)^(?P<indent>[ \t]*)if\s*\(\s*{condition}\s*\)"
    )
    checks = list(check.finditer(text))
    if len(checks) != 1:
        raise PatchError(f"expected one {variable} check, found {len(checks)}")
    found = checks[0]
    line_end = text.find("\n", found.end())
    if line_end < 0:
        line_end = len(text)
    opening = text.find("{", found.end(), line_end)
    if opening >= 0:
        end = _matching_brace(text, opening) + 1
    else:
        semicolon = text.find(";", found.end(), line_end)
        if semicolon < 0:
            raise PatchError(f"could not locate the end of the {variable} check")
        end = semicolon + 1
    original_check = text[found.start() : end]
    if not any(token in original_check for token in result_tokens):
        raise PatchError(f"unexpected {variable} mismatch result")
    text = (
        text[: found.start()]
        + found.group("indent")
        + f"// {marker}: accept matching V8 release lines across embedders."
        + text[end:]
    )
    return declaration.sub(
        lambda declaration_match: declaration_match.group("indent")
        + f"// {marker}: cached header value intentionally ignored.",
        text,
        count=1,
    )


def _ensure_include(text: str, include: str) -> str:
    directive = f"#include {include}"
    if directive in text:
        return text
    # d8.cc has generated #include blocks near the end of some releases.  The
    # last include in the file may therefore be inside a namespace or #if.
    # Insert immediately before the top-level V8 namespace instead.
    namespace = re.search(r"^namespace\s+v8\s*\{", text, flags=re.MULTILINE)
    if not namespace:
        raise PatchError(f"cannot insert include {include}: namespace v8 missing")
    return text[: namespace.start()] + directive + "\n\n" + text[namespace.start() :]


def _type_traversal(features: Features) -> str:
    if features.object_style == "raw-pointer":
        return """      i::Object* object = constants->get(index);
      if (object->IsSharedFunctionInfo()) {
        pending.push_back(
            i::handle(i::SharedFunctionInfo::cast(object), isolate));
      }"""
    if features.object_style == "tagged":
        declaration = "auto object = constants->get(index);"
    else:
        declaration = "i::Object object = constants->get(index);"
    predicate = (
        "i::IsSharedFunctionInfo(object)"
        if features.object_predicate_style == "free"
        else "object.IsSharedFunctionInfo()"
    )
    return f"""      {declaration}
      if ({predicate}) {{
        pending.push_back(
            i::handle(i::SharedFunctionInfo::cast(object), isolate));
      }}"""


def _loadjsc_definition(features: Features) -> str:
    utf8 = (
        "String::Utf8Value filename(args.GetIsolate(), args[file_index]);"
        if features.utf8_value_needs_isolate
        else "String::Utf8Value filename(args[file_index]);"
    )
    read_chars = (
        "ReadChars(args.GetIsolate(), *filename, &length)"
        if features.read_chars_needs_isolate
        else "ReadChars(*filename, &length)"
    )
    deserialize_args = "isolate, &cached_data, source"
    origin = ""
    if features.origin_options:
        origin = "\n    ScriptOriginOptions origin_options;"
        deserialize_args += ", origin_options"
    get_bytecode = {
        "get-isolate": "current->GetBytecodeArray(isolate)",
        "get": "current->GetBytecodeArray()",
        "field": "current->bytecode_array()",
    }[features.bytecode_accessor]
    traversal = _type_traversal(features)

    return f"""

// {PATCH_MARKER}: crash-safe loader for source-less V8 code caches.
void Shell::LoadJSC(const FunctionCallbackInfo<Value>& args) {{
  i::Isolate* isolate =
      reinterpret_cast<i::Isolate*>(args.GetIsolate());
  i::HandleScope handle_scope(isolate);

  for (int file_index = 0; file_index < args.Length(); ++file_index) {{
    {utf8}
    if (*filename == nullptr) {{
      args.GetIsolate()->ThrowException(Exception::Error(
          String::NewFromUtf8(args.GetIsolate(), "Invalid JSC filename",
                              NewStringType::kNormal)
              .ToLocalChecked()));
      return;
    }}

    int length = 0;
    std::unique_ptr<char[]> file_data({read_chars});
    if (!file_data || length <= 0) {{
      args.GetIsolate()->ThrowException(Exception::Error(
          String::NewFromUtf8(args.GetIsolate(), "Error reading JSC file",
                              NewStringType::kNormal)
              .ToLocalChecked()));
      return;
    }}

    i::{features.cache_type} cached_data(
        reinterpret_cast<const uint8_t*>(file_data.get()), length);
    i::Handle<i::String> source =
        isolate->factory()->NewStringFromAsciiChecked("source");{origin}
    i::MaybeHandle<i::SharedFunctionInfo> maybe_function =
        i::CodeSerializer::Deserialize({deserialize_args});
    i::Handle<i::SharedFunctionInfo> function;
    if (!maybe_function.ToHandle(&function)) {{
      args.GetIsolate()->ThrowException(Exception::Error(
          String::NewFromUtf8(
              args.GetIsolate(),
              "JSC deserialization failed (wrong V8 version, flags, or corrupt data)",
              NewStringType::kNormal)
              .ToLocalChecked()));
      return;
    }}

    // Print each function exactly once.  Nested functions are emitted as flat
    // blocks and linked by address by View8, avoiding recursive ShortPrint
    // expansion and the stack-overflow cycle reported in issue #23.
    std::vector<i::Handle<i::SharedFunctionInfo>> pending;
    std::vector<i::Handle<i::SharedFunctionInfo>> printed;
    pending.push_back(function);
    while (!pending.empty()) {{
      i::Handle<i::SharedFunctionInfo> current = pending.back();
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

      auto bytecode = i::handle({get_bytecode}, isolate);
      std::cout << "\\nStart SharedFunctionInfo\\n";
      current->SharedFunctionInfoPrint(std::cout);
      std::cout << "\\nStart BytecodeArray\\n";
      bytecode->Disassemble(std::cout);
      std::cout << "\\nEnd BytecodeArray\\n";
      std::cout << "End SharedFunctionInfo\\n" << std::flush;

      auto constants = i::handle(bytecode->constant_pool(), isolate);
      for (int index = 0; index < constants->length(); ++index) {{
{traversal}
      }}
    }}
  }}
}}
"""


def patch_d8_cc(text: str, features: Features) -> str:
    if PATCH_MARKER in text:
        return text
    for include in (
        '"src/snapshot/code-serializer.h"',
        "<iostream>",
        "<memory>",
        "<vector>",
    ):
        text = _ensure_include(text, include)

    read_chars_match = re.search(
        r"(?:static\s+)?char\s*\*\s*(?:Shell::)?ReadChars\s*\(", text
    )
    if not read_chars_match:
        raise PatchError("could not find ReadChars definition")
    opening = text.find("{", read_chars_match.end())
    if opening < 0:
        raise PatchError("could not find ReadChars function body")
    closing = _matching_brace(text, opening)
    insertion = closing + 1
    text = text[:insertion] + _loadjsc_definition(features) + text[insertion:]

    create_global = text.find("Shell::CreateGlobalTemplate")
    if create_global < 0:
        raise PatchError("could not find CreateGlobalTemplate anchor")
    create_opening = text.find("{", create_global)
    create_closing = _matching_brace(text, create_opening)
    registration_start = text.rfind(
        "  return global_template;", create_opening, create_closing
    )
    if registration_start < 0:
        raise PatchError("could not find CreateGlobalTemplate return anchor")
    registration = f"""  // {PATCH_MARKER}: public d8 entry point.
  global_template->Set(
      String::NewFromUtf8(isolate, "loadjsc", NewStringType::kNormal)
          .ToLocalChecked(),
      FunctionTemplate::New(isolate, LoadJSC));
"""
    return text[:registration_start] + registration + text[registration_start:]


def patch_d8_h(text: str) -> str:
    if "static void LoadJSC(" in text:
        return text
    anchor = re.search(
        r"^(\s*)static\s+(?:Maybe)?Local<String>\s+ReadFile\s*\(",
        text,
        flags=re.MULTILINE,
    )
    if not anchor:
        raise PatchError("could not find Shell::ReadFile declaration")
    declaration = (
        f"{anchor.group(1)}static void LoadJSC("
        "const FunctionCallbackInfo<Value>& args);\n"
    )
    return text[: anchor.start()] + declaration + text[anchor.start() :]


def patch_serializer(text: str, features: Features) -> str:
    if "JSC2JS_SOURCE_HASH_BYPASS" not in text:
        if features.sanity_style in {"split", "split-readonly-checksum"}:
            old = "return SanityCheckJustSource(expected_source_hash);"
            if text.count(old) != 1:
                raise PatchError(
                    "expected one split SanityCheckJustSource return, found "
                    f"{text.count(old)}"
                )
            new = (
                "// JSC2JS_SOURCE_HASH_BYPASS: .jsc has no original source text.\n"
                "  return result;"
            )
            text = text.replace(old, new, 1)
        else:
            declaration_pattern = re.compile(
                r"(?m)^(?P<indent>[ \t]*)uint32_t\s+source_hash\s*=\s*"
                r"GetHeaderValue\(kSourceHashOffset\);[ \t]*$"
            )
            check_pattern = re.compile(
                r"(?m)^(?P<indent>[ \t]*)if\s*\(\s*"
                r"source_hash\s*!=\s*(?:expected_source_hash|"
                r"SourceHash\(source\))\s*\)\s*"
                r"return\s+SOURCE_MISMATCH;[ \t]*$"
            )
            declarations = list(declaration_pattern.finditer(text))
            checks = list(check_pattern.finditer(text))
            if len(declarations) != 1 or len(checks) != 1:
                raise PatchError(
                    "expected one inline source hash declaration/check, found "
                    f"{len(declarations)}/{len(checks)}"
                )
            text = declaration_pattern.sub(
                lambda found: found.group("indent")
                + "// JSC2JS_SOURCE_HASH_BYPASS: .jsc has no original source text.",
                text,
                count=1,
            )
            text = check_pattern.sub(
                lambda found: found.group("indent")
                + "// Source hash comparison intentionally omitted.",
                text,
                count=1,
            )

    if "JSC2JS_VERSION_HASH_BYPASS" not in text:
        text = _remove_hash_mismatch_check(
            text,
            variable="version_hash",
            offset="kVersionHashOffset",
            condition=r"version_hash\s*!=\s*Version::Hash\(\)",
            result_tokens=("VERSION_MISMATCH", "kVersionMismatch"),
            marker="JSC2JS_VERSION_HASH_BYPASS",
        )
    if "JSC2JS_FLAGS_HASH_BYPASS" not in text:
        text = _remove_hash_mismatch_check(
            text,
            variable="flags_hash",
            offset="kFlagHashOffset",
            condition=r"flags_hash\s*!=\s*FlagList::Hash\(\)",
            result_tokens=("FLAGS_MISMATCH", "kFlagsMismatch"),
            marker="JSC2JS_FLAGS_HASH_BYPASS",
        )
    return text


def patch_string_printer(text: str) -> str:
    if "JSC2JS_FULL_STRING_PRINT" in text:
        return text
    function = text.find("void String::StringShortPrint")
    if function < 0:
        raise PatchError("StringShortPrint function was not found")
    opening = text.find("{", function)
    closing = _matching_brace(text, opening)
    body = text[opening:closing]
    condition = re.search(
        r"if\s*\(([^\n)]*kMaxShortPrintLength[^\n)]*)\)", body
    )
    if not condition:
        raise PatchError("StringShortPrint truncation condition was not found")
    replacement = (
        "if (false && ("
        + condition.group(1)
        + "))  /* JSC2JS_FULL_STRING_PRINT */"
    )
    patched_body = body[: condition.start()] + replacement + body[condition.end() :]
    return text[:opening] + patched_body + text[closing:]


def transform_sources(
    sources: dict[str, str]
) -> tuple[dict[str, str], Features, list[str]]:
    features = detect_features(sources)
    d8_cc_path, d8_cc = _select(sources, D8_CC_PATHS, "Shell::CreateGlobalTemplate")
    d8_h_path, d8_h = _select(sources, D8_H_PATHS, "class Shell")
    string_path, string_source = _select(
        sources, STRING_PATHS, "String::StringShortPrint"
    )

    result = dict(sources)
    result[d8_cc_path] = patch_d8_cc(d8_cc, features)
    result[d8_h_path] = patch_d8_h(d8_h)
    result[SERIALIZER_CC] = patch_serializer(sources[SERIALIZER_CC], features)
    result[string_path] = patch_string_printer(string_source)
    changed = [
        path for path in result if result[path] != sources.get(path)
    ]
    expected = {d8_cc_path, d8_h_path, SERIALIZER_CC, string_path}
    if set(changed) != expected:
        raise PatchError(
            f"unexpected changed files: expected={sorted(expected)} actual={sorted(changed)}"
        )
    return result, features, sorted(changed)


def load_tree(root: Path) -> dict[str, str]:
    paths = set(
        D8_CC_PATHS
        + D8_H_PATHS
        + PRINTER_PATHS
        + HEAP_PATHS
        + STRING_PATHS
        + SFI_PATHS
        + FIXED_ARRAY_PATHS
        + OBJECTS_H_PATHS
        + (SERIALIZER_H, SERIALIZER_CC)
    )
    return _read_existing(root, sorted(paths))


def apply_to_tree(root: Path, report_path: Path) -> dict:
    sources = load_tree(root)
    transformed, features, changed = transform_sources(sources)
    for relative in changed:
        (root / relative).write_text(transformed[relative], encoding="utf-8", newline="\n")
    report = {
        "success": True,
        "root": str(root),
        "family": features.family_name,
        "features": asdict(features),
        "changed_files": changed,
        "safety": {
            "source_hash_bypassed": True,
            "version_and_flags_hashes_bypassed": True,
            "upstream_cache_checks_preserved": True,
            "upstream_checks_present": upstream_protections(
                sources[SERIALIZER_CC]
            ),
            "deserializer_modified": False,
            "recursive_short_print_modified": False,
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--report", type=Path, default=Path("jsc2js_legacy_patch_report.json")
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
        print(f"[legacy-patch] ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
