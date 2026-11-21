"""Adds the project sub-folders to the import path so modules keep their bare names."""
import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
for _d in ("interface", "web", "agent", "core", ""):
    _p = os.path.join(ROOT, _d) if _d else ROOT
    if _p not in sys.path:
        sys.path.insert(0, _p)
