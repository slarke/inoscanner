"""Append-only file logger writing a per-run file into a ``log/`` folder."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path


class FileLogger:
    """Appends timestamped lines to a per-session file under ``log/``.

    The log directory is created on startup; one file is opened per run
    (``plc_panel_YYYYMMDD_HHMMSS.log``).  Falls back to the system temp dir if
    the configured directory cannot be created.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        filename: str | None = None,
    ) -> None:
        base = Path(directory) if directory else Path.cwd() / "log"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            base = Path(tempfile.gettempdir())
        if filename is None:
            filename = time.strftime("plc_panel_%Y%m%d_%H%M%S.log")
        self.path = base / filename

    def append(self, line: str) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # Disk logging is best-effort; the on-screen journal still works.
            pass
