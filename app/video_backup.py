"""Compatibility import; implementation lives in the Huilima plugin.

Kept during the staged host migration. This is the SAME module object, so
existing imports, monkeypatches and shared state do not fork or duplicate.
"""
from importlib import import_module as _import_module
import sys as _sys

_impl = _import_module("huilima_plugin.video_backup")
if __name__ == "__main__" and hasattr(_impl, "main"):
    raise SystemExit(_impl.main())
_sys.modules[__name__] = _impl
