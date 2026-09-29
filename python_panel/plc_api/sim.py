"""Эмулятор текущего MAIN.LD — реализует тот же интерфейс, что и транспорт.

Нужен для тестов и для запуска API без железа (``python -m plc_api --simulate``).
Моделирует только то, что есть в ладдере:

* MC-блоки срабатывают по переднему фронту Execute-катушки и только при
  включённом MC_Power; MoveAbsolute/MoveVelocity/MoveRelative запрещены
  нормально-закрытым контактом защёлки блокировки оси;
* концевики (discrete inputs) по фронту взводят защёлку M100/M200/M300,
  импульс M101/M201/M301 её снимает; пока защёлка взведена, удерживается MC_Stop;
* импульс замера по приезду — только ось X при M14;
* позиция меняется линейно со скоростью (мм/с), без разгона.

Это модель для проверки логики API, а не эталон реального привода.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from plc_panel.codec import float_to_words, words_to_float
from plc_panel.config import AXES, AXIS_SAFETY, IO

from .ladder import PULSE_AXES
from .transport import TransportError


@dataclass
class _AxisModel:
    position: float = 0.0
    speed: float = 0.0
    mode: Optional[str] = None      # None | "target" | "velocity"
    target: float = 0.0
    pulse_pending: bool = False


class SimulatedPlc:
    """Программная модель ПЛК с «регистровым» интерфейсом ModbusTransport."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._coils: Dict[int, bool] = {}
        self._prev_coils: Dict[int, bool] = {}
        self._regs: Dict[int, int] = {}
        self._inputs: Dict[int, bool] = {}
        self._prev_inputs: Dict[int, bool] = {}
        self._axes = {n: _AxisModel() for n in AXES}
        self._t = time.monotonic()
        self._open = False
        #: Внедрение сбоев для тестов.
        self.connect_failures = 0
        self.read_failures = 0
        #: Сколько импульсов замера выдано на Y0 (ось X, M14).
        self.y0_pulses = 0
        self.y0_level = False

    # ------------------------------------------------------------------ #
    # Интерфейс ModbusTransport
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        with self._lock:
            if self.connect_failures > 0:
                self.connect_failures -= 1
                raise TransportError("sim: ПЛК недоступен")
            self._open = True

    def close(self) -> None:
        with self._lock:
            self._open = False

    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def read_holding(self, address: int, count: int) -> List[int]:
        with self._lock:
            self._before_read()
            return [self._regs.get(address + i, 0) for i in range(count)]

    def read_coils(self, address: int, count: int) -> List[bool]:
        with self._lock:
            self._before_read()
            return [self._coils.get(address + i, False) for i in range(count)]

    def read_discrete(self, address: int, count: int) -> List[bool]:
        with self._lock:
            self._before_read()
            return [self._inputs.get(address + i, False) for i in range(count)]

    def write_coil(self, address: int, value: bool) -> None:
        with self._lock:
            self._require_open()
            self._coils[address] = bool(value)
            self._scan()

    def write_registers(self, address: int, values: Sequence[int]) -> None:
        with self._lock:
            self._require_open()
            for i, v in enumerate(values):
                self._regs[address + i] = int(v) & 0xFFFF
            self._scan()

    # ------------------------------------------------------------------ #
    # Управление моделью из тестов
    # ------------------------------------------------------------------ #
    def set_input(self, address: int, value: bool) -> None:
        """Выставить физический вход Xn (концевик)."""
        with self._lock:
            self._inputs[address] = bool(value)
            self._scan()

    def drop_link(self) -> None:
        """Имитировать обрыв TCP со стороны ПЛК."""
        with self._lock:
            self._open = False

    def position(self, axis: int) -> float:
        with self._lock:
            self._scan()
            return self._axes[axis].position

    def coil(self, address: int) -> bool:
        with self._lock:
            return self._coils.get(address, False)

    # ------------------------------------------------------------------ #
    # «Скан» ладдера
    # ------------------------------------------------------------------ #
    def _require_open(self) -> None:
        if not self._open:
            raise TransportError("sim: соединение закрыто")

    def _before_read(self) -> None:
        self._require_open()
        if self.read_failures > 0:
            self.read_failures -= 1
            raise TransportError("sim: нет ответа")
        self._scan()

    def _rising(self, coil: int) -> bool:
        return self._coils.get(coil, False) and not self._prev_coils.get(coil, False)

    def _real(self, address: int) -> float:
        return words_to_float([self._regs.get(address, 0), self._regs.get(address + 1, 0)])

    def _put_real(self, address: int, value: float) -> None:
        lo, hi = float_to_words(value)
        self._regs[address], self._regs[address + 1] = lo, hi

    def _scan(self) -> None:
        now = time.monotonic()
        dt, self._t = now - self._t, now

        for n, r in AXES.items():
            s = AXIS_SAFETY[n]
            m = self._axes[n]

            for addr in (s.limit_left, s.limit_right):
                if self._inputs.get(addr, False) and not self._prev_inputs.get(addr, False):
                    self._coils[s.block_latch] = True
            if self._coils.get(s.block_reset, False):
                self._coils[s.block_latch] = False
            blocked = self._coils.get(s.block_latch, False)
            powered = self._coils.get(r.power, False)

            if powered and not blocked:
                if self._rising(r.abs_exec):
                    m.mode, m.target = "target", self._real(r.abs_pos)
                    m.speed = abs(self._real(r.abs_spd))
                    m.pulse_pending = (n in PULSE_AXES
                                       and self._coils.get(r.abs_pulse_en, False))
                if self._rising(r.rel_exec):
                    m.mode = "target"
                    m.target = m.position + self._real(r.rel_distance)
                    m.speed = abs(self._real(r.rel_speed))
                    m.pulse_pending = False
                if self._rising(r.vel_exec):
                    m.mode, m.speed = "velocity", self._real(r.vel_block)
            if self._rising(r.set_pos_exec) and m.mode is None:
                m.position = self._real(r.set_pos_val)
            if not powered or blocked or self._coils.get(r.stop_exec, False):
                m.mode = None

            velocity = 0.0
            if m.mode == "target":
                step = m.speed * dt
                delta = m.target - m.position
                if abs(delta) <= step:
                    m.position, m.mode = m.target, None
                    if m.pulse_pending:
                        self.y0_pulses += 1
                        m.pulse_pending = False
                else:
                    velocity = m.speed if delta > 0 else -m.speed
                    m.position += velocity * dt
            elif m.mode == "velocity":
                velocity = m.speed
                m.position += velocity * dt

            self._put_real(r.act_pos, m.position)
            self._put_real(r.act_vel, velocity)

        if self._coils.get(IO.Y0_SET, False):
            self.y0_level = True
        if self._coils.get(IO.Y0_RESET, False):
            self.y0_level = False

        self._prev_coils = dict(self._coils)
        self._prev_inputs = dict(self._inputs)
