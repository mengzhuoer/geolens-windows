"""GeoLens Desktop launcher entry point (frozen by PyInstaller)."""
from __future__ import annotations

import multiprocessing
import os
import sys

# PyInstaller's one-folder build adds _internal to sys.path already; the
# desktop package sits next to this module in the bundle.
from desktop.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
