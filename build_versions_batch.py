#!/usr/bin/env python3
"""Backward-compatible entry point for the source-aware V8 batch builder.

The implementation lives in ``build_versions_batch_v3.py``.  Keeping this
small wrapper prevents older local commands from selecting archived patch
generations while preserving the historical script name.
"""

from build_versions_batch_v3 import main


if __name__ == "__main__":
    raise SystemExit(main())
