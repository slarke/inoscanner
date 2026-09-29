"""Общая настройка тестов: пути импорта и headless-Qt.

`core` (servo_core) импортируется из корня репозитория, `plc_panel` — из
`python_panel/`. Qt работает с offscreen-платформой, поэтому тесты не требуют
дисплея.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "python_panel"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def wait_until(cond: Callable[[], bool], timeout: float = 3.0,
               step: float = 0.01) -> bool:
    """Ждать выполнения условия (для потоков ядра). True, если дождались."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


@pytest.fixture(scope="session")
def qapp():
    """Единственный QCoreApplication на всю сессию тестов."""
    from PyQt5.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    yield app
