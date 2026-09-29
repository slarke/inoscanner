"""Convenience launcher so the app can be started with ``python main.py``."""

from __future__ import annotations

import sys

from plc_panel.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
