"""Development entry point for the desktop launcher."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from desktop.app import main  # noqa: E402

if __name__ == "__main__":
    main()
