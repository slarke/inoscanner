"""Shared colour palette and the application stylesheet (QSS)."""

from __future__ import annotations

# Brand / axis colours reused across widgets.
COLOR_X = "#0078d4"
COLOR_Y = "#107c41"
COLOR_Z = "#7a24db"
COLOR_DANGER = "#d83b01"
COLOR_WARN = "#d83b01"

# Discrete-input LED colours (Home X, Home Y, VNA Ready, E-Stop).
INPUT_COLORS = [COLOR_Y, COLOR_Y, COLOR_X, COLOR_DANGER]
# Discrete-output LED colours (Y0, Y1, Y2).
OUTPUT_COLORS = [COLOR_DANGER, COLOR_Y, COLOR_Z]

STYLESHEET = """
QWidget {
    font-family: 'Segoe UI', Tahoma, sans-serif;
    font-size: 13px;
    color: #333;
}
QMainWindow, #appRoot { background: #f3f3f3; }

QFrame#card {
    background: white;
    border: 1px solid #e5e5e5;
    border-radius: 4px;
}
QLabel#cardTitle { font-size: 15px; font-weight: 600; }
QLabel#sectionTitle { font-size: 13px; font-weight: 600; color: #444; }

QPushButton {
    padding: 6px 12px;
    border: none;
    border-radius: 4px;
    font-weight: bold;
    color: white;
    background: #0078d4;
}
QPushButton:hover { background: #005a9e; }
QPushButton[kind="success"] { background: #107c41; }
QPushButton[kind="success"]:hover { background: #0b592e; }
QPushButton[kind="danger"] { background: #d83b01; }
QPushButton[kind="danger"]:hover { background: #b83201; }
QPushButton[kind="warn"] {
    background: #fff2cc; color: #d83b01; border: 1px solid #d83b01;
}
QPushButton[kind="x"] { background: #0078d4; }
QPushButton[kind="y"] { background: #107c41; }
QPushButton[kind="z"] { background: #7a24db; }

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    padding: 4px 6px;
    border: 1px solid #ccc;
    border-radius: 4px;
    background: white;
}

QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    padding: 8px 34px;
    margin: 0 4px;
    min-width: 60px;
    font-weight: bold;
    background: #e0e0e0;
    border: none;
}
QTabBar::tab:first { margin-left: 6px; }
QTabBar::tab:last { margin-right: 6px; }
QTabBar::tab:selected {
    background: #f3f3f3;
    border-bottom: 3px solid #0078d4;
    color: #0078d4;
}
QTabWidget::pane { border: none; }

#logTitle { color: #333; font-weight: 600; }
QPlainTextEdit#logOutput {
    background: #ffffff;
    color: #1e1e1e;
    border: 1px solid #d0d0d0;
    font-family: 'Consolas', monospace;
    font-size: 11px;
}
#logPanel { background: #f3f3f3; }
"""
