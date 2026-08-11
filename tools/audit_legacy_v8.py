#!/usr/bin/env python3
"""Audit the V8 source APIs used by jsc2js without cloning the V8 repo.

The script discovers V8 versions shipped by Node.js or Electron, intersects
them with real V8 tags, and downloads only the handful of source files touched
by the jsc2js patch.  The resulting JSON contains one record per exact V8 tag
plus compatibility groups derived from source/API fingerprints.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import determine_versions  # noqa: E402
from patches.legacy.apply_legacy_patch import (  # noqa: E402
    PatchError,
    fixed_array_object_style,
    object_type_predicate_style,
    shared_function_info_bytecode_accessor,
)


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")
RAW_ROOT = "https://raw.githubusercontent.com/v8/v8"

SOURCE_CANDIDATES = {
    "d8_cc": ("src/d8/d8.cc", "src/d8.cc"),
    "d8_h": ("src/d8/d8.h", "src/d8.h"),
    "printer": (
        "src/diagnostics/objects-printer.cc",
        "src/objects/objects-printer.cc",
        "src/objects-printer.cc",
    ),
    "heap": (
        "src/diagnostics/objects-printer.cc",
        "src/objects/objects.cc",
        "src/objects.cc",
    ),
    "string": ("src/objects/string.cc", "src/objects.cc"),
    "sfi_h": (
        "src/objects/shared-function-info.h",
        "src/objects/shared-function-info-inl.h",
        "src/objects.h",
    ),
    "fixed_array_h": (
        "src/objects/fixed-array.h",
        "src/objects.h",
    ),
    "objects_h": (
        "src/objects/objects.h",
        "src/objects.h",
    ),
    "serializer_h": ("src/snapshot/code-serializer.h",),
    "serializer_cc": ("src/snapshot/code-serializer.cc",),
    "deserializer_cc": ("src/snapshot/deserializer.cc",),
    "object_deserializer_cc": ("src/snapshot/object-deserializer.cc",),
    "v8gen": ("tools/dev/v8gen.py",),
}

SOURCE_MARKERS = {
    "d8_cc": "Shell::CreateGlobalTemplate",
    "d8_h": "class Shell",
    "printer": "SharedFunctionInfo::SharedFunctionInfoPrint",
    "heap": "HeapObject::HeapObjectShortPrint",
    "string": "String::StringShortPrint",
    "sfi_h": "BytecodeArray",
    "fixed_array_h": "FixedArray",
    "objects_h": "class Object",
    "serializer_h": "CodeSerializer",
    "serializer_cc": "CodeSerializer::Deserialize",
    # Older branches use a non-template Deserializer, newer ones use
    # Deserializer<IsolateT>; file existence is the stable check.
    "object_deserializer_cc": "ObjectDeserializer::Deserialize",
}


def version_key(version: str) -> tuple[int, ...]:
    parts = tuple(int(part) for part in version.split("."))
    return parts + (0,) * (4 - len(parts))


def normalize_signature(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_signature(text: str, marker: str, terminator: str = "{") -> str:
    start = text.find(marker)
    if start < 0:
        return ""
    line_start = text.rfind("\n", 0, start) + 1
    end = text.find(terminator, start)
    if end < 0 or end - line_start > 1600:
        end = min(len(text), line_start + 1600)
    return normalize_signature(text[line_start:end])


def git_remote_tags(repo_url: str) -> set[str]:
    output = subprocess.check_output(
        ["git", "ls-remote", "--tags", repo_url], text=True
    )
    tags: set[str] = set()
    for line in output.splitlines():
        if "refs/tags/" not in line:
            continue
        tag = line.split("refs/tags/", 1)[1].split("^", 1)[0]
        if SEMVER_RE.fullmatch(tag):
            tags.add(tag)
    return tags


def discover_versions(minimum: str, maximum_exclusive: str) -> list[str]:
    candidates = (
        determine_versions.fetch_node_v8_versions()
        | determine_versions.fetch_electron_v8_versions()
    )
    tags = git_remote_tags("https://github.com/v8/v8.git")
    lower = version_key(minimum)
    upper = version_key(maximum_exclusive)
    return sorted(
        (
            version
            for version in candidates & tags
            if lower <= version_key(version) < upper
        ),
        key=version_key,
    )


class RawSourceCache:
    def __init__(self, directory: Path, retries: int = 3):
        self.directory = directory
        self.retries = retries
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, version: str, source_path: str) -> Path:
        digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:16]
        return self.directory / version / f"{digest}.txt"

    @staticmethod
    def _read_published(path: Path) -> str:
        # Windows can briefly reject a reader while another process atomically
        # replaces this path.  The window is normally below one scheduler tick.
        for attempt in range(20):
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.01)
        raise AssertionError("unreachable")

    def get(self, version: str, source_path: str) -> str | None:
        cached = self._path(version, source_path)
        missing = cached.with_suffix(".missing")
        if cached.exists():
            return self._read_published(cached)
        if missing.exists():
            return None

        url = f"{RAW_ROOT}/{version}/{source_path}"
        for attempt in range(self.retries):
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "jsc2js-v8-source-audit"}
                )
                with urllib.request.urlopen(request, timeout=45) as response:
                    content = response.read().decode("utf-8", errors="replace")
                cached.parent.mkdir(parents=True, exist_ok=True)
                # The API audit and semantic replay may intentionally run in
                # parallel against the same cache.  Publish a complete file
                # atomically so the other process cannot observe a truncated
                # first write.
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=cached.parent,
                        prefix=f"{cached.name}.",
                        suffix=".tmp",
                        delete=False,
                    ) as stream:
                        stream.write(content)
                        temporary = Path(stream.name)
                    try:
                        os.replace(temporary, cached)
                    except OSError:
                        # On Windows another process may already have
                        # atomically published, then opened, this same URL.
                        # Its complete target is authoritative; only suppress
                        # the replacement error when that target now contains
                        # the exact immutable response we fetched.
                        existing = (
                            self._read_published(cached)
                            if cached.is_file()
                            else None
                        )
                        if existing != content:
                            raise
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return content
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    missing.parent.mkdir(parents=True, exist_ok=True)
                    missing.touch()
                    return None
                if error.code not in {429, 500, 502, 503, 504}:
                    raise
            except (TimeoutError, urllib.error.URLError):
                pass
            time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"failed to fetch {url}")


def first_source(
    cache: RawSourceCache,
    version: str,
    candidates: Iterable[str],
    marker: str | None = None,
) -> tuple[str, str] | tuple[None, None]:
    for path in candidates:
        content = cache.get(version, path)
        if content is not None and (marker is None or marker in content):
            return path, content
    return None, None


def classify_version(cache: RawSourceCache, version: str) -> dict:
    paths: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    errors: list[str] = []

    try:
        for category, candidates in SOURCE_CANDIDATES.items():
            path, content = first_source(
                cache, version, candidates, SOURCE_MARKERS.get(category)
            )
            paths[category] = path
            if content is not None:
                sources[category] = content
    except Exception as error:  # preserve the per-version failure in the report
        return {"version": version, "status": "fetch-error", "error": str(error)}

    for required in (
        "d8_cc",
        "d8_h",
        "printer",
        "heap",
        "string",
        "fixed_array_h",
        "objects_h",
        "serializer_h",
        "serializer_cc",
        "deserializer_cc",
    ):
        if required not in sources:
            errors.append(f"missing:{required}")

    joined = "\n".join(sources.values())
    d8 = sources.get("d8_cc", "")
    printer = sources.get("printer", "")
    heap = sources.get("heap", "")
    string = sources.get("string", "")
    serializer_h = sources.get("serializer_h", "")
    serializer_cc = sources.get("serializer_cc", "")
    sfi_h = sources.get("sfi_h", "")
    fixed_array_h = sources.get("fixed_array_h", "")
    objects_h = sources.get("objects_h", "")

    deserialize_signature = extract_signature(
        serializer_h, "Deserialize(", terminator=";"
    )
    deserialize_impl = extract_signature(
        serializer_cc, "CodeSerializer::Deserialize("
    )

    if "AlignedCachedData" in deserialize_signature:
        cache_type = "AlignedCachedData"
    elif "ScriptData" in deserialize_signature:
        cache_type = "ScriptData"
    else:
        cache_type = "unknown"
        errors.append("unknown:cache-type")

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

    try:
        object_style = fixed_array_object_style(fixed_array_h)
    except PatchError:
        object_style = "unknown"
        errors.append("unknown:fixed-array-get")

    try:
        predicate_style = object_type_predicate_style(objects_h)
    except PatchError:
        predicate_style = "unknown"
        errors.append("unknown:object-predicate")

    try:
        bytecode_accessor = shared_function_info_bytecode_accessor(sfi_h)
    except PatchError:
        bytecode_accessor = "unknown"
        errors.append("unknown:sfi-bytecode-accessor")

    if not any(token in joined for token in ("BytecodeArray", "bytecode_array")):
        errors.append("missing:bytecode-array")
    if "CodeSerializer::Deserialize(" not in serializer_cc:
        errors.append("missing:deserialize-implementation")
    if "SharedFunctionInfo::SharedFunctionInfoPrint" not in printer:
        errors.append("missing:sfi-printer")
    if "HeapObject::HeapObjectShortPrint" not in heap:
        errors.append("missing:heap-short-printer")
    if "String::StringShortPrint" not in string:
        errors.append("missing:string-short-printer")
    if "Shell::CreateGlobalTemplate" not in d8:
        errors.append("missing:d8-global-template")

    api = {
        "layout": "split-d8" if paths.get("d8_cc") == "src/d8/d8.cc" else "flat-d8",
        "cache_type": cache_type,
        "deserialize_signature": deserialize_signature,
        "deserialize_impl": deserialize_impl,
        "has_origin_options": "ScriptOriginOptions" in deserialize_signature,
        "has_cached_script": "maybe_cached_script" in deserialize_signature,
        "sanity_style": sanity_style,
        "object_style": object_style,
        "object_predicate_style": predicate_style,
        "bytecode_accessor": bytecode_accessor,
        "flags_style": "v8_flags" if "v8_flags." in serializer_cc else "FLAG_",
        "string_show_details": "StringShortPrint(StringStream* accumulator, bool"
        in string,
        "has_object_deserializer": "object_deserializer_cc" in sources,
        "has_v8gen": "v8gen" in sources,
    }

    family_material = json.dumps(api, sort_keys=True, separators=(",", ":"))
    family = hashlib.sha256(family_material.encode("utf-8")).hexdigest()[:12]

    source_hash = hashlib.sha256()
    for category in sorted(sources):
        if category == "v8gen":
            continue
        source_hash.update(category.encode("utf-8"))
        source_hash.update(b"\0")
        source_hash.update(sources[category].encode("utf-8"))
        source_hash.update(b"\0")

    return {
        "version": version,
        "status": "ok" if not errors else "incompatible",
        "paths": paths,
        "api": api,
        "family": family,
        "source_fingerprint": source_hash.hexdigest()[:16],
        "errors": errors,
    }


def family_summary(records: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(record.get("family", "fetch-error"), []).append(record)

    result = []
    for family, members in groups.items():
        versions = [item["version"] for item in members]
        result.append(
            {
                "family": family,
                "count": len(members),
                "first": min(versions, key=version_key),
                "last": max(versions, key=version_key),
                "representatives": sorted(versions, key=version_key)[:1]
                + sorted(versions, key=version_key)[-1:],
                "api": members[0].get("api"),
                "statuses": sorted({item["status"] for item in members}),
            }
        )
    return sorted(result, key=lambda item: version_key(item["first"]))


def write_markdown(path: Path, payload: dict) -> None:
    scope = payload["scope"]
    scope_note = (
        "Scope: exact V8 tags shipped by Node.js or Electron with "
        f"{scope['minimum']} <= V8 < {scope['maximum_exclusive']}."
    )
    if version_key(scope["maximum_exclusive"]) <= version_key("5.1.0"):
        boundary_note = (
            f"Compatibility result: all {payload['summary']['incompatible']} tags "
            "lack the complete code-cache deserialization path required by jsc2js."
        )
    else:
        boundary_note = (
            "V8 5.1 is the supported lower boundary; the separate pre-5.1 audit "
            "records why older tags are incompatible."
        )
    lines = [
        "# Legacy V8 API audit",
        "",
        f"Exact tags audited: **{payload['summary']['versions']}**",
        "",
        f"API families: **{payload['summary']['families']}**",
        "",
        scope_note,
        "",
        boundary_note,
        "",
        "| Family | Range | Tags | Layout | Cache | Deserialize | Sanity | Objects | Predicate |",
        "|---|---:|---:|---|---|---|---|---|---|",
    ]
    for family in payload["families"]:
        api = family.get("api") or {}
        signature = api.get("deserialize_signature", "").replace("|", "\\|")
        if len(signature) > 90:
            signature = signature[:87] + "..."
        lines.append(
            "| `{family}` | {first}–{last} | {count} | {layout} | {cache} | "
            "`{signature}` | {sanity} | {objects} | {predicate} |".format(
                family=family["family"],
                first=family["first"],
                last=family["last"],
                count=family["count"],
                layout=api.get("layout", ""),
                cache=api.get("cache_type", ""),
                signature=signature,
                sanity=api.get("sanity_style", ""),
                objects=api.get("object_style", ""),
                predicate=api.get("object_predicate_style", ""),
            )
        )
    lines.extend(
        [
            "",
            "The JSON report contains the source paths, exact API fingerprint, and "
            "compatibility result for every audited tag.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-version", default="5.1.0")
    parser.add_argument("--max-version-exclusive", default="12.0.0")
    parser.add_argument("--versions-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(tempfile.gettempdir()) / "jsc2js-v8-source-audit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.versions_file:
        versions = json.loads(args.versions_file.read_text(encoding="utf-8"))
        if not isinstance(versions, list) or not all(
            isinstance(version, str) and SEMVER_RE.fullmatch(version)
            for version in versions
        ):
            raise ValueError("versions file must contain a JSON array of V8 tags")
        versions = sorted(set(versions), key=version_key)
    else:
        versions = discover_versions(args.min_version, args.max_version_exclusive)

    print(f"[audit] exact V8 tags: {len(versions)}")
    cache = RawSourceCache(args.cache_dir)
    records: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(classify_version, cache, version): version
            for version in versions
        }
        for completed, future in enumerate(
            concurrent.futures.as_completed(futures), start=1
        ):
            record = future.result()
            records.append(record)
            if completed % 20 == 0 or completed == len(futures):
                print(f"[audit] {completed}/{len(futures)}")

    records.sort(key=lambda item: version_key(item["version"]))
    families = family_summary(records)
    payload = {
        "scope": {
            "minimum": args.min_version,
            "maximum_exclusive": args.max_version_exclusive,
            "sources": ["node", "electron"],
            "v8_repository": "https://github.com/v8/v8",
        },
        "summary": {
            "versions": len(records),
            "families": len(families),
            "ok": sum(record["status"] == "ok" for record in records),
            "incompatible": sum(
                record["status"] == "incompatible" for record in records
            ),
            "fetch_errors": sum(
                record["status"] == "fetch-error" for record in records
            ),
        },
        "families": families,
        "versions": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if args.markdown_output:
        write_markdown(args.markdown_output, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0 if payload["summary"]["fetch_errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
