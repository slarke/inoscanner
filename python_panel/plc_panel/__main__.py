"""Application entry point: ``python -m plc_panel``."""

from __future__ import annotations

import sys

from PyQt5.QtWidgets import QApplication

from .config import AppSettings
from .service import PlcService
from .ui.main_window import MainWindow
from .ui.styles import STYLESHEET


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)

    settings = AppSettings.load()   # auto-load from app_config.json
    service = PlcService()
    window = MainWindow(service, settings)
    window.show()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
