"""Base directory for user-writable files (config, logs, queue DB, session).

When running from source this is the ``python_panel`` directory.  In a
PyInstaller build (``sys.frozen``) module files live inside the bundle
(``_internal`` / a temp dir for one-file), which is read-only / ephemeral, so
user files must live next to the executable instead.
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_base_dir() -> Path:
    """Directory that holds app_config.json, log/, scan_queue.db, sessions."""
    if getattr(sys, "frozen", False):           # PyInstaller bundle
        return Path(sys.executable).resolve().parent
    # Source layout: …/python_panel/plc_panel/paths.py -> …/python_panel
    return Path(__file__).resolve().parent.parent
