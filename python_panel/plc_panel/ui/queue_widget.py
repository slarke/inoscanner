"""Editable scan-sequence list with mouse drag-and-drop reordering."""

from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..models import ScanStep, StepType
from .common import button, double_spin, int_spin

_AXIS_NAMES = {1: "X", 2: "Y", 3: "Z"}


class _DragHandle(QLabel):
    """Grip that starts a row drag when pressed with the left button."""

    def __init__(self, queue: "ScenarioQueueWidget", row: QFrame) -> None:
        super().__init__("☰")
        self._queue = queue
        self._row = row
        self.setCursor(Qt.OpenHandCursor)
        self.setFixedWidth(28)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            "background: #eaeaea; border: 1px solid #ccc; border-radius: 3px;"
            " font-weight: bold; color: #555;")

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self._queue.begin_drag(self._row)
            event.accept()
        else:
            super().mousePressEvent(event)


class ScenarioQueueWidget(QWidget):
    """Owns the working list of :class:`ScanStep` and renders editable rows.

    Rows bind their editors directly to the underlying step objects, so edits
    take effect without an index lookup.  Reordering is done by dragging a row's
    grip: the model list and the row widgets are moved together, never rebuilt,
    so editor state and the chosen start step are preserved.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._steps: List[ScanStep] = []
        self._rows: List[QFrame] = []
        self._active_index = -1
        self._drag_row: Optional[QFrame] = None
        self._start_group = QButtonGroup(self)
        self._start_group.setExclusive(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setMinimumHeight(360)
        self._scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._rows_host = QWidget()
        self._rows_host.installEventFilter(self)
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(6, 6, 6, 6)
        self._rows_layout.setSpacing(4)
        self._rows_layout.addStretch(1)
        self._scroll.setWidget(self._rows_host)
        outer.addWidget(self._scroll)

    # ------------------------------------------------------------------ #
    # Public model API                                                   #
    # ------------------------------------------------------------------ #
    def steps(self) -> List[ScanStep]:
        return self._steps

    def add_step(self, step: ScanStep) -> None:
        self._steps.append(step)
        self._rebuild()

    def set_steps(self, steps: List[ScanStep]) -> None:
        self._steps = list(steps)
        self._active_index = -1
        self._rebuild()

    def clear(self) -> None:
        self._steps.clear()
        self._active_index = -1
        self._rebuild()

    def start_index(self) -> int:
        index = self._start_group.checkedId()
        return index if index >= 0 else 0

    def set_active_index(self, index: int) -> None:
        self._active_index = index
        for row in self._rows:
            self._style_row(row)
        # Auto-scroll: keep the active step in view as the scenario advances.
        if 0 <= index < len(self._rows):
            self._scroll.ensureWidgetVisible(self._rows[index], 0, 40)

    # ------------------------------------------------------------------ #
    # Drag-and-drop reordering                                           #
    # ------------------------------------------------------------------ #
    def begin_drag(self, row: QFrame) -> None:
        self._drag_row = row
        self._style_row(row)
        self._rows_host.grabMouse()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        if obj is self._rows_host and self._drag_row is not None:
            if event.type() == QEvent.MouseMove:
                self._drag_to(event.pos().y())
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._end_drag()
                return True
        return super().eventFilter(obj, event)

    def _drag_to(self, y: int) -> None:
        if not self._rows:
            return
        current = self._rows.index(self._drag_row)
        target = current
        for i, row in enumerate(self._rows):
            geo = row.geometry()
            if geo.top() <= y <= geo.bottom():
                target = i
                break
        else:
            if y < self._rows[0].geometry().top():
                target = 0
            elif y > self._rows[-1].geometry().bottom():
                target = len(self._rows) - 1
        if target != current:
            self._reorder(current, target)

    def _reorder(self, frm: int, to: int) -> None:
        self._steps.insert(to, self._steps.pop(frm))
        row = self._rows.pop(frm)
        self._rows.insert(to, row)
        self._rows_layout.removeWidget(row)
        self._rows_layout.insertWidget(to, row)
        self._renumber()

    def _end_drag(self) -> None:
        self._rows_host.releaseMouse()
        dragged, self._drag_row = self._drag_row, None
        if dragged is not None:
            self._style_row(dragged)

    # ------------------------------------------------------------------ #
    # Row mutation                                                       #
    # ------------------------------------------------------------------ #
    def _delete(self, step: ScanStep) -> None:
        # Удаляем строго тот объект, чья кнопка нажата (по identity, а не по ==).
        # ScanStep — dataclass со значимым __eq__: при одинаковых полях (частый
        # случай после импорта JSON) list.remove() убрал бы первый равный шаг,
        # а не выбранный. Поэтому ищем по `is`.
        for i, existing in enumerate(self._steps):
            if existing is step:
                del self._steps[i]
                self._rebuild()
                return

    # ------------------------------------------------------------------ #
    # Rendering                                                          #
    # ------------------------------------------------------------------ #
    def _rebuild(self) -> None:
        for old in list(self._start_group.buttons()):
            self._start_group.removeButton(old)
        while self._rows_layout.count() > 1:
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._rows = []
        for index, step in enumerate(self._steps):
            row = self._build_row(index, step)
            self._rows.append(row)
            self._rows_layout.insertWidget(index, row)

    def _renumber(self) -> None:
        for index, row in enumerate(self._rows):
            row.number_label.setText(f"№{index + 1:02d}")
            self._start_group.removeButton(row.radio)
            self._start_group.addButton(row.radio, index)
            self._style_row(row)

    def _style_row(self, row: QFrame) -> None:
        dragging = row is self._drag_row
        active = row in self._rows and self._rows.index(row) == self._active_index
        if dragging:
            bg, border = "#fff2e6", "1px solid #d83b01"
        elif active:
            bg, border = "#f0f7ff", "2px solid #0078d4"
        else:
            bg, border = "white", "1px solid #e0e0e0"
        row.setStyleSheet(
            "QFrame#stepRow { background: %s; border: %s; border-radius: 4px; }"
            % (bg, border))

    def _build_row(self, index: int, step: ScanStep) -> QFrame:
        row = QFrame()
        row.setObjectName("stepRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(6)

        radio = QRadioButton()
        radio.setToolTip("Начать сценарий с этого шага")
        self._start_group.addButton(radio, index)
        row.radio = radio
        layout.addWidget(radio)

        handle = _DragHandle(self, row)
        layout.addWidget(handle)

        number_label = QLabel(f"№{index + 1:02d}")
        row.number_label = number_label
        layout.addWidget(number_label)

        layout.addLayout(self._build_editors(step))
        layout.addStretch(1)

        delete = button("🗑", "danger", on_click=lambda: self._delete(step))
        delete.setFixedWidth(30)
        layout.addWidget(delete)

        self._style_row(row)
        return row

    def _build_editors(self, step: ScanStep) -> QHBoxLayout:
        box = QHBoxLayout()
        box.setSpacing(4)

        if step.is_move:
            axis = QComboBox()
            for number, name in _AXIS_NAMES.items():
                axis.addItem(f"Ось {name}", number)
            axis.setCurrentIndex(step.axis - 1)
            axis.currentIndexChanged.connect(
                lambda _i, c=axis: setattr(step, "axis", c.currentData()))

            pos = double_spin(step.pos, width=70)
            pos.valueChanged.connect(lambda v: setattr(step, "pos", v))
            spd = double_spin(step.spd, width=60)
            spd.valueChanged.connect(lambda v: setattr(step, "spd", v))

            badge = QLabel("+⚡ импульс" if step.type is StepType.MOVE_PULSE
                           else "без имп.")
            badge.setStyleSheet(
                "color: %s; font-size: 10px; font-weight: bold;"
                % ("#d83b01" if step.type is StepType.MOVE_PULSE else "#666"))

            for widget in (QLabel("Ось:"), axis, QLabel("Поз:"), pos,
                           QLabel("V:"), spd, badge):
                box.addWidget(widget)

        elif step.type is StepType.DELAY:
            ms = int_spin(step.ms, width=80)
            ms.valueChanged.connect(lambda v: setattr(step, "ms", v))
            box.addWidget(QLabel("⏱ Пауза:"))
            box.addWidget(ms)
            box.addWidget(QLabel("мс"))

        elif step.type is StepType.SCPI:
            cmd = QLineEdit(step.cmd)
            cmd.setMinimumWidth(160)
            cmd.textChanged.connect(lambda t: setattr(step, "cmd", t))
            box.addWidget(QLabel("🔌 SCPI:"))
            box.addWidget(cmd)

        elif step.type is StepType.PULSE:
            box.addWidget(QLabel("⚡ Аппаратный импульс (выход Y0)"))

        return box
