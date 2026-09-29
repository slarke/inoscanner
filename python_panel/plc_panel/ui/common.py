"""Small factory helpers to keep widget construction declarative and DRY."""

from __future__ import annotations

from typing import Optional

from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


def button(text: str, kind: str = "", on_click=None) -> QPushButton:
    """A styled push button. ``kind`` maps to a QSS property selector."""
    btn = QPushButton(text)
    if kind:
        btn.setProperty("kind", kind)
    if on_click is not None:
        btn.clicked.connect(on_click)
    return btn


def card(title: str = "", accent: str = "") -> tuple[QFrame, QVBoxLayout]:
    """A white "card" frame. Returns the frame and its content layout."""
    frame = QFrame()
    frame.setObjectName("card")
    if accent:
        frame.setStyleSheet(f"#card {{ border-top: 3px solid {accent}; }}")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(6)
    if title:
        label = QLabel(title)
        label.setObjectName("cardTitle")
        layout.addWidget(label)
    return frame, layout


def section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionTitle")
    return label


def double_spin(value: float, step: float = 0.1,
                width: Optional[int] = None) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(-1_000_000.0, 1_000_000.0)
    spin.setDecimals(2)
    spin.setSingleStep(step)
    spin.setValue(value)
    if width:
        spin.setFixedWidth(width)
    return spin


def int_spin(value: int, maximum: int = 1_000_000,
             width: Optional[int] = None) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(0, maximum)
    spin.setValue(value)
    if width:
        spin.setFixedWidth(width)
    return spin


def led(diameter: int = 14) -> QLabel:
    """A round indicator label; recolour with :func:`set_led`."""
    dot = QLabel()
    dot.setFixedSize(diameter, diameter)
    set_led(dot, False, "#cccccc")
    return dot


def set_led(dot: QLabel, on: bool, color: str) -> None:
    fill = color if on else "#cccccc"
    glow = f"; border: 1px solid {color}" if on else ""
    radius = dot.width() // 2
    dot.setStyleSheet(
        f"background-color: {fill}; border-radius: {radius}px{glow};"
    )


def hline_separator() -> QWidget:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line
