"""Custom-painted gauges: the XY plane plot and per-axis linear scales."""

from __future__ import annotations

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QSizePolicy, QWidget

from ..config import GridLimits


def _format_tick(value: float) -> str:
    """Compact tick label, switching to scientific notation when needed."""
    if abs(value) >= 10000 or (0 < abs(value) < 0.01):
        return f"{value:.1e}"
    return str(round(value))


class XYPlotWidget(QWidget):
    """2-D plot of the current X/Y position inside the configured grid."""

    def __init__(self, limits: GridLimits, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._limits = limits
        self._x = 0.0
        self._y = 0.0
        self._cursor = None  # last hovered pixel position (QPoint) or None
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)  # hover without a pressed button

    def set_position(self, x: float, y: float) -> None:
        self._x, self._y = x, y
        self.update()

    def refresh(self) -> None:
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._cursor = event.pos()
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._cursor = None
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        lim = self._limits
        w, h, pad = self.width(), self.height(), 40

        painter.fillRect(self.rect(), QColor("#fcfcfc"))
        painter.fillRect(QRectF(pad, pad, w - 2 * pad, h - 2 * pad), QColor("#ffffff"))

        x_range = lim.x_max - lim.x_min or 1.0
        y_range = lim.y_max - lim.y_min or 1.0

        def sx(v: float) -> float:
            return pad + (v - lim.x_min) / x_range * (w - 2 * pad)

        def sy(v: float) -> float:
            return (h - pad) - (v - lim.y_min) / y_range * (h - 2 * pad)

        painter.setFont(QFont("Segoe UI", 7))
        grid_pen = QPen(QColor("#e5e5e5"))
        grid_pen.setStyle(Qt.DashLine)

        for i in range(11):
            xv = lim.x_min + i * x_range / 10
            px = sx(xv)
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(px, pad), QPointF(px, h - pad))
            painter.setPen(QColor("#555"))
            painter.drawText(QRectF(px - 20, h - pad + 2, 40, 12),
                             Qt.AlignHCenter, _format_tick(xv))

            yv = lim.y_min + i * y_range / 10
            py = sy(yv)
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(pad, py), QPointF(w - pad, py))
            painter.setPen(QColor("#555"))
            painter.drawText(QRectF(0, py - 6, pad - 6, 12),
                             Qt.AlignRight | Qt.AlignVCenter, _format_tick(yv))

        painter.setPen(QPen(QColor("#333"), 2))
        painter.drawLine(QPointF(pad, h - pad), QPointF(w - pad, h - pad))
        painter.drawLine(QPointF(pad, pad), QPointF(pad, h - pad))

        cx = min(max(sx(self._x), pad), w - pad)
        cy = min(max(sy(self._y), pad), h - pad)
        painter.setBrush(QColor("#0078d4"))
        painter.setPen(QPen(QColor("#fff"), 2))
        painter.drawEllipse(QPointF(cx, cy), 8, 8)

        # Hover read-out: crosshair + the data coordinates under the cursor.
        if self._cursor is not None:
            mx, my = self._cursor.x(), self._cursor.y()
            if pad <= mx <= w - pad and pad <= my <= h - pad:
                data_x = lim.x_min + (mx - pad) / (w - 2 * pad) * x_range
                data_y = lim.y_min + (h - pad - my) / (h - 2 * pad) * y_range

                cross = QPen(QColor("#0078d4"))
                cross.setStyle(Qt.DotLine)
                painter.setPen(cross)
                painter.drawLine(QPointF(pad, my), QPointF(w - pad, my))
                painter.drawLine(QPointF(mx, pad), QPointF(mx, h - pad))

                text = f"X: {data_x:.2f}   Y: {data_y:.2f}"
                painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
                box_w, box_h = 130, 20
                bx = mx + 12 if mx + 12 + box_w <= w - pad else mx - 12 - box_w
                by = my - 12 - box_h if my - 12 - box_h >= pad else my + 12
                painter.setBrush(QColor(0, 0, 0, 190))
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(QRectF(bx, by, box_w, box_h), 4, 4)
                painter.setPen(QColor("#ffffff"))
                painter.drawText(QRectF(bx, by, box_w, box_h), Qt.AlignCenter, text)


class LinearAxisWidget(QWidget):
    """Horizontal gauge showing one axis position within its min/max range."""

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self._value = 0.0
        self._min = -50.0
        self._max = 500.0
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_range(self, minimum: float, maximum: float) -> None:
        self._min, self._max = minimum, maximum
        self.update()

    def set_value(self, value: float) -> None:
        self._value = value
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fcfcfc"))

        w, h, pad = self.width(), self.height(), 30
        track_y = h / 2

        painter.setPen(QPen(QColor("#e0e0e0"), 4))
        painter.drawLine(QPointF(pad, track_y), QPointF(w - pad, track_y))

        rng = self._max - self._min or 1.0
        painter.setFont(QFont("Segoe UI", 7))
        for i in range(6):
            val = self._min + i * rng / 5
            px = pad + i * (w - 2 * pad) / 5
            painter.setPen(QPen(QColor("#aaaaaa"), 1))
            painter.drawLine(QPointF(px, track_y - 5), QPointF(px, track_y + 5))
            painter.setPen(QColor("#555"))
            painter.drawText(QRectF(px - 25, track_y + 6, 50, 12),
                             Qt.AlignHCenter, _format_tick(val))

        clamped = min(max(self._value, self._min), self._max)
        marker_x = pad + (clamped - self._min) / rng * (w - 2 * pad)
        painter.setBrush(QColor(self._color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(marker_x, track_y), 7, 7)
