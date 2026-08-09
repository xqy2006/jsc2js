#!/usr/bin/env python3
"""Backward-compatible entry point for the source-aware V8 batch builder."""

from build_versions_batch_v3 import main


if __name__ == "__main__":
    raise SystemExit(main())
