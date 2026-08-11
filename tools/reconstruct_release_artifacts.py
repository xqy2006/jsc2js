#!/usr/bin/env python3
"""Reconstruct per-platform release directories from matrix artifacts."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


ARTIFACT_DIR_RE = re.compile(
    r"^d8-(?P<version>\d+\.\d+\.\d+(?:\.\d+)?)-(?P<platform>Linux|Windows)$"
)
PLATFORM_BINARY = {"Linux": "d8", "Windows": "d8.exe"}
COMPANION_FILES = (
    "snapshot_blob.bin",
    "apply_patch_report.txt",
    "apply_patch_report.json",
)


def reconstruct_artifacts(input_dir: Path, output_dir: Path) -> list[Path]:
    """Copy recognized matrix outputs into their release staging directories."""
    output_dir.mkdir(parents=True, exist_ok=True)
    reconstructed: list[Path] = []
    seen: set[str] = set()

    binaries = sorted(
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.name in PLATFORM_BINARY.values()
    )
    for binary in binaries:
        artifact_dir = binary.parent
        match = ARTIFACT_DIR_RE.fullmatch(artifact_dir.name)
        if match is None:
            continue

        platform_name = match.group("platform")
        expected_binary = PLATFORM_BINARY[platform_name]
        if binary.name != expected_binary:
            raise RuntimeError(
                f"Unexpected {platform_name} binary name in {artifact_dir}: "
                f"expected {expected_binary}, found {binary.name}"
            )
        if artifact_dir.name in seen:
            raise RuntimeError(f"Duplicate release artifact: {artifact_dir.name}")
        seen.add(artifact_dir.name)

        target_dir = output_dir / artifact_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target_dir / expected_binary)
        for name in COMPANION_FILES:
            companion = artifact_dir / name
            if companion.is_file():
                shutil.copy2(companion, target_dir / name)

        reconstructed.append(target_dir)
        print(f"Reconstructed {artifact_dir.name}")

    print(f"Reconstructed {len(reconstructed)} platform artifact directories.")
    return reconstructed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("downloaded"))
    parser.add_argument("--output", type=Path, default=Path("stage"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reconstruct_artifacts(args.input, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
