"""Persistent scan-session state for crash recovery.

The running scenario (its steps and the current step index) is mirrored to disk
so that an unexpected program termination can be resumed automatically on the
next launch.  The file is removed when a scenario finishes, is stopped or
aborted — so it only survives a real crash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

from .models import ScanStep
from .paths import app_base_dir

SESSION_PATH = app_base_dir() / "scan_session.json"


class ScanSession:
    """Disk-backed snapshot of the in-progress scan scenario."""

    def __init__(self, path: str | Path = SESSION_PATH) -> None:
        self._path = Path(path)
        self._payload: Optional[dict] = None

    def begin(self, steps: List[ScanStep], index: int) -> None:
        """Record the steps and starting index of a freshly started run."""
        self._payload = {
            "running": True,
            "index": index,
            "steps": [s.to_dict() for s in steps],
        }
        self._flush()

    def update(self, index: int) -> None:
        """Persist the new active step index (called as the run advances)."""
        if self._payload is None:
            return
        self._payload["index"] = index
        self._flush()

    def end(self) -> None:
        """Clear the session (normal finish / stop / abort)."""
        self._payload = None
        try:
            self._path.unlink()
        except OSError:
            pass

    def load(self) -> Optional[Tuple[List[ScanStep], int]]:
        """Return ``(steps, index)`` of an interrupted run, or ``None``."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not data.get("running"):
            return None
        steps = [s for s in (ScanStep.from_dict(d) for d in data.get("steps", []))
                 if s]
        if not steps:
            return None
        index = int(data.get("index", 0))
        index = max(0, min(index, len(steps) - 1))
        return steps, index

    def _flush(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            # Best-effort: crash-resume is a convenience, not a correctness gate.
            pass
