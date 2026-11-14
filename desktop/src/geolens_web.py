"""Entry point for the standalone frontend static server."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from desktop.web_server import main

if __name__ == "__main__":
    main()
