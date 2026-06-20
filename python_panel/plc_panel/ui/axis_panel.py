"""Reusable per-axis debug card used on the 'Motor debug' tab."""

from __future__ import annotations

from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget

from ..config import AxisRegisters, GridLimits
from ..models import AxisTelemetry
from ..service import PlcService
from .charts import LinearAxisWidget
from .common import button, double_spin, section


class AxisDebugPanel(QWidget):
    """Full diagnostic + manual control card for a single axis."""

    def __init__(self, axis: int, registers: AxisRegisters, color: str,
                 service: PlcService, limits: GridLimits,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._axis = axis
        self._color = color
        self._service = service
        self._limits = limits

        from .common import card  # local import avoids a cycle at module load
        frame, layout = card(f"Тесты: Ось {registers.name}", accent=color)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(frame)

        self._stats = QLabel("Реальные: 0.00 мм")
        self._stats.setStyleSheet(f"font-weight: bold; color: {color};")
        self._raw = QLabel(f"D{registers.act_pos}: [0, 0]")
        self._raw.setStyleSheet("color: #666; font-family: Consolas; font-size: 11px;")
        self._errors = QLabel("Ошибки: Нет")
        self._errors.setStyleSheet("color: #666; font-weight: bold; font-size: 11px;")
        for label in (self._stats, self._raw, self._errors):
            layout.addWidget(label)

        self._gauge = LinearAxisWidget(color)
        self._gauge.set_range(*self._axis_range())
        layout.addWidget(self._gauge)

        layout.addLayout(self._power_row())
        layout.addWidget(section("Позиционирование (MC_MoveAbsolute)"))
        self._abs_pos = double_spin(50.0)
        self._abs_spd = double_spin(5.0)
        layout.addLayout(self._labeled("Коорд (мм):", self._abs_pos))
        layout.addLayout(self._labeled("Скорость:", self._abs_spd))
        layout.addWidget(button("MOVE", "", self._move_abs))

        layout.addWidget(section("Смещение координат (MC_SetPosition)"))
        self._set_val = double_spin(0.0)
        layout.addLayout(self._labeled("Новая поз:", self._set_val))
        offset_row = QHBoxLayout()
        offset_row.addWidget(button("Применить", "warn", self._set_position))
        offset_row.addWidget(button("Сброс в 0.00", "danger", self._reset_position))
        layout.addLayout(offset_row)

        layout.addWidget(section("Режим скорости (MC_MoveVelocity)"))
        self._vel_spd = double_spin(10.0)
        self._vel_acc = double_spin(20.0)
        self._vel_dec = double_spin(20.0)
        layout.addLayout(self._labeled("V скорость:", self._vel_spd))
        accel_row = self._labeled("Разгон / Зам:", self._vel_acc)
        accel_row.addWidget(self._vel_dec)
        layout.addLayout(accel_row)
        cruise_row = QHBoxLayout()
        cruise_row.addWidget(button("КРУИЗ", "", self._move_velocity))
        cruise_row.addWidget(button("STOP", "danger", self._stop))
        layout.addLayout(cruise_row)

        layout.addWidget(section("Смещение на величину (MC_MoveRelative)"))
        self._rel_dist = double_spin(10.0)
        self._rel_spd = double_spin(5.0)
        layout.addLayout(self._labeled("Distance (мм):", self._rel_dist))
        layout.addLayout(self._labeled("Velocity:", self._rel_spd))
        layout.addWidget(button("MOVE REL", "", self._move_relative))
        layout.addStretch(1)

    # ------------------------------------------------------------------ #
    # Telemetry refresh                                                  #
    # ------------------------------------------------------------------ #
    def update_telemetry(self, telemetry: AxisTelemetry) -> None:
        self._stats.setText(f"Реальные: {telemetry.position:.2f} мм")
        self._raw.setText(
            self._raw.text().split(":")[0] + f": {telemetry.position_raw}")
        if telemetry.has_error:
            self._errors.setText(
                f"Ошибки: servo={telemetry.servo_error}, axis={telemetry.axis_error}")
            self._errors.setStyleSheet("color: #d83b01; font-weight: bold;")
        else:
            self._errors.setText("Ошибки: Нет")
            self._errors.setStyleSheet("color: #666; font-weight: bold;")
        self._gauge.set_value(telemetry.position)

    def refresh_range(self) -> None:
        self._gauge.set_range(*self._axis_range())

    # ------------------------------------------------------------------ #
    # Layout helpers                                                     #
    # ------------------------------------------------------------------ #
    def _power_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(button("ON", "success",
                             lambda: self._service.set_power(self._axis, True)))
        row.addWidget(button("OFF", "danger",
                             lambda: self._service.set_power(self._axis, False)))
        row.addWidget(button("Сброс", "warn",
                             lambda: self._service.reset_axis(self._axis)))
        return row

    @staticmethod
    def _labeled(text: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel(text))
        row.addWidget(widget)
        return row

    def _axis_range(self) -> tuple[float, float]:
        if self._axis == 3:
            return self._limits.z_min, self._limits.z_max
        return self._limits.x_min, self._limits.x_max

    # ------------------------------------------------------------------ #
    # Command handlers                                                   #
    # ------------------------------------------------------------------ #
    def _move_abs(self) -> None:
        self._service.move_absolute(
            self._axis, self._abs_pos.value(), self._abs_spd.value(), False)

    def _move_relative(self) -> None:
        self._service.move_relative(
            self._axis, self._rel_dist.value(), self._rel_spd.value())

    def _set_position(self) -> None:
        self._service.set_position(self._axis, self._set_val.value())

    def _reset_position(self) -> None:
        self._service.set_position(self._axis, 0.0)

    def _move_velocity(self) -> None:
        self._service.move_velocity(
            self._axis, self._vel_spd.value(),
            self._vel_acc.value(), self._vel_dec.value())

    def _stop(self) -> None:
        self._service.stop_axis(self._axis, self._vel_dec.value())
