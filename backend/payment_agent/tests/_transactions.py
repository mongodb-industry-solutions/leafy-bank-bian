"""Load a transactions module by file path, for parity tests.

payment_agent cannot import transactions code at runtime, so mirrored logic is pinned by
loading the original here. The transactions root goes on `sys.path` only while loading (its
modules import `shared.*` / `contexts.*`); those packages are then dropped from `sys.modules`
so nothing else in this suite resolves against them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TRANSACTIONS = Path(__file__).resolve().parents[2] / "transactions"
_PREFIXES = ("shared", "contexts")


def load(relative_path: str, name: str):
    before = set(sys.modules)
    sys.path.insert(0, str(TRANSACTIONS))
    try:
        spec = importlib.util.spec_from_file_location(f"_tx_{name}", TRANSACTIONS / relative_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolve their module while executing
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(TRANSACTIONS))
        for key in set(sys.modules) - before:
            if key.split(".")[0] in _PREFIXES:
                del sys.modules[key]
