"""bookport's compiled DjVu decoders (the optional bookport-native package), when they're installed.

Each decoder keeps its pure-Python code and hands the work to the compiled one when ``native``
has it: the same results, many times faster. ``BOOKPORT_PURE=1`` keeps bookport on the Python
code (the tests run both ways); a bookport-native of another version is left alone, since its
decoders may not match this bookport's."""

from __future__ import annotations

import os

VERSION = "0.3.0"  # the bookport-native this bookport is made for

native = None
if not os.environ.get("BOOKPORT_PURE"):
    try:
        import bookport_native as _native
    except ImportError:
        pass
    else:
        if getattr(_native, "__version__", None) == VERSION:
            native = _native


def accelerated() -> bool:
    """Whether the compiled DjVu decoders are in use."""
    return native is not None
