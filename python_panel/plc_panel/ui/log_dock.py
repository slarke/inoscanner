"""Dockable system-journal widget.

The PLC journal is a :class:`QDockWidget`: docked to the side of the main
window by default, but the operator can drag it to another edge, float it out
as its own window, or close it (and reopen via the «Журнал» toggle).
:class:`MainWindow` forwards every log line here.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QDockWidget,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class LogDock(QDockWidget):
    """Dock widget holding the read-only PLC journal."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Системный журнал ПЛК", parent)
        self.setObjectName("logDock")
        self.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
            | Qt.BottomDockWidgetArea)
        self.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable)

        content = QWidget()
        content.setObjectName("logPanel")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)

        header = QHBoxLayout()
        self._autoscroll = QCheckBox("Автопрокрутка")
        self._autoscroll.setChecked(True)
        header.addWidget(self._autoscroll)
        header.addStretch(1)
        clear = QPushButton("Очистить")
        clear.clicked.connect(self.clear)
        header.addWidget(clear)
        layout.addLayout(header)

        self._output = QPlainTextEdit(readOnly=True)
        self._output.setObjectName("logOutput")
        self._output.setMaximumBlockCount(5000)  # bound memory on long sessions
        layout.addWidget(self._output)

        self.setWidget(content)

    # ------------------------------------------------------------------ #
    def append(self, line: str) -> None:
        bar = self._output.verticalScrollBar()
        prev = bar.value()
        self._output.appendPlainText(line)
        # Follow the tail only when auto-scroll is on; otherwise keep the
        # operator's current scroll position.
        bar.setValue(bar.maximum() if self._autoscroll.isChecked() else prev)

    def clear(self) -> None:
        self._output.clear()
