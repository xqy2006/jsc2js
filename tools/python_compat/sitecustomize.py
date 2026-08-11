"""Compatibility aliases for Python modules vendored by historical V8."""

from __future__ import absolute_import

import collections

try:
    import collections.abc as collections_abc
except ImportError:  # Python 2
    collections_abc = collections


for name in (
    "Awaitable",
    "Callable",
    "Container",
    "Coroutine",
    "Generator",
    "Hashable",
    "Iterable",
    "Iterator",
    "Mapping",
    "MutableMapping",
    "MutableSequence",
    "Sequence",
    "Set",
    "Sized",
):
    if not hasattr(collections, name) and hasattr(collections_abc, name):
        setattr(collections, name, getattr(collections_abc, name))
