"""Modbus register map and application defaults.

Mirrors the layout of the original ``config.rs`` so the PLC program does not
need to change.  Addresses are raw Modbus addresses (the same numbers the PLC
exposes as ``M``-coils and ``D``-registers).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from .paths import app_base_dir

#: Directory for user files — next to the .exe when frozen, else python_panel.
_BASE_DIR = app_base_dir()

#: User-editable settings file, loaded automatically on startup.
CONFIG_PATH = _BASE_DIR / "app_config.json"


@dataclass(frozen=True)
class AxisRegisters:
    """Register addresses for a single servo axis.

    ``REAL`` (32-bit float) values occupy two consecutive 16-bit registers
    stored low-word first (see :mod:`plc_panel.codec`).
    """

    name: str

    # Command coils (M).
    power: int          # MC_Power
    vel_exec: int       # MC_MoveVelocity execute
    reset: int          # MC_Reset
    abs_exec: int       # MC_MoveAbsolute execute
    abs_pulse_en: int   # emit measurement pulse on "Done"
    read_error_en: int  # MC_ReadAxisError enable
    stop_exec: int      # MC_Stop execute
    set_pos_exec: int   # MC_SetPosition execute
    rel_exec: int       # MC_MoveRelative execute

    # Parameter registers (D).
    vel_block: int      # speed, accel, decel (3 x REAL, contiguous)
    set_pos_val: int    # MC_SetPosition target (REAL)
    abs_pos: int        # MC_MoveAbsolute position (REAL)
    abs_spd: int        # MC_MoveAbsolute speed (REAL)
    stop_dec: int       # MC_Stop deceleration (REAL)
    rel_distance: int   # MC_MoveRelative distance (REAL)
    rel_speed: int      # MC_MoveRelative velocity (REAL)

    # Telemetry registers (D).
    act_pos: int        # actual position (REAL)
    act_vel: int        # actual velocity (REAL)
    error_block: int    # servo error id at [0], axis error id at [2]


#: Axis number (1=X, 2=Y, 3=Z) -> register map.
AXES: Dict[int, AxisRegisters] = {
    # Axis X is mapped exactly from MAIN.LD (Axis_0):
    #   Coils  - M10 Power, M11 MoveVelocity, M12 Reset, M13 MoveAbsolute,
    #            M14 pulse-on-Done (TPR), M15 ReadAxisError, M16 Stop,
    #            M17 SetPosition, M19 MoveRelative.
    #   Reg.   - D10/D12/D14 velocity block, D16/D18 MoveAbsolute pos/vel,
    #            D20/D22 MoveRelative distance/vel, D24 SetPosition,
    #            D210/D214 actual pos/vel, D220/D222 servo/axis error.
    # MC_Stop's deceleration is a named soft element in MAIN.LD (not a D
    # register), so stop_dec keeps the original design's D50.
    #
    # MAIN.LD currently programs ONLY Axis_0 (X).  The Y/Z maps below are the
    # authoritative *contract* the still-to-be-written Y/Z ladder must follow:
    # they were re-laid-out into clean, non-overlapping register windows.  The
    # earlier placeholder maps collided (e.g. Y act_pos D220 overran X's error
    # block D220/D222, and rel/abs REALs overlapped within an axis).  See
    # ../PLC_LADDER_REQUIREMENTS.md.  Layout (each REAL = 2 words):
    #   params  - X D10-D25(+D50)   Y D100-D117   Z D130-D147
    #   telem   - X D210/D220       Y D230/D240   Z D250/D260
    #   coils   - X M10-M19         Y M20-M29     Z M30-M39
    1: AxisRegisters(
        name="X",
        power=10, vel_exec=11, reset=12, abs_exec=13, abs_pulse_en=14,
        read_error_en=15, stop_exec=16, set_pos_exec=17, rel_exec=19,
        vel_block=10, set_pos_val=24, abs_pos=16, abs_spd=18, stop_dec=50,
        rel_distance=20, rel_speed=22,
        act_pos=210, act_vel=214, error_block=220,
    ),
    2: AxisRegisters(
        name="Y",
        power=20, vel_exec=21, reset=22, abs_exec=23, abs_pulse_en=24,
        read_error_en=25, stop_exec=26, set_pos_exec=27, rel_exec=29,
        vel_block=100, set_pos_val=114, abs_pos=106, abs_spd=108, stop_dec=116,
        rel_distance=110, rel_speed=112,
        act_pos=230, act_vel=234, error_block=240,
    ),
    3: AxisRegisters(
        name="Z",
        power=30, vel_exec=31, reset=32, abs_exec=33, abs_pulse_en=34,
        read_error_en=35, stop_exec=36, set_pos_exec=37, rel_exec=39,
        vel_block=130, set_pos_val=144, abs_pos=136, abs_spd=138, stop_dec=146,
        rel_distance=140, rel_speed=142,
        act_pos=250, act_vel=254, error_block=260,
    ),
}


@dataclass(frozen=True)
class AxisSafety:
    """Аппаратная блокировка одной оси по двум концевым выключателям.

    Каждая ось имеет левый и правый концевик (физические входы ПЛК,
    читаются как Modbus discrete inputs, FC02).  Срабатывание любого
    концевика на ПЛК защёлкивает катушку ``block_latch`` (`X → SET Mxxx`),
    что останавливает движение.  Импульс на ``block_reset`` снимает защёлку
    (`Mxxx → RST`).  Софт читает ``block_latch`` как признак «заблокировано»
    и пишет ``block_reset`` по кнопке «Сброс блокировки».
    """

    name: str           # имя оси (X / Y / Z) для подписей
    limit_left: int     # discrete-input адрес левого концевика (Xn)
    limit_right: int    # discrete-input адрес правого концевика (Xn)
    block_latch: int    # M-coil: признак "заблокировано" (чтение)
    block_reset: int    # M-coil: импульс снятия блокировки (запись)


#: Ось (1=X, 2=Y, 3=Z) -> аппаратная блокировка по концевикам.  Адреса взяты
#: из спецификации task.txt:
#:   X: концевики X1/X2, блокировка M100, сброс M101;
#:   Y: концевики X3/X4, блокировка M200, сброс M201;
#:   Z: концевики X5/X6, блокировка M300, сброс M301.
#: Входы Xn читаются как discrete inputs по адресу n (X1=1 … X6=6).
AXIS_SAFETY: Dict[int, AxisSafety] = {
    1: AxisSafety("X", limit_left=1, limit_right=2, block_latch=100, block_reset=101),
    2: AxisSafety("Y", limit_left=3, limit_right=4, block_latch=200, block_reset=201),
    3: AxisSafety("Z", limit_left=5, limit_right=6, block_latch=300, block_reset=301),
}


class IO:
    """Global diagnostic and Easy521 discrete I/O addresses."""

    BLOCK_ERROR_ID = 200   # shared ErrorID for all motion blocks (D200)

    Y0_SET = 50            # M50 - set output Y0 (measurement trigger)
    Y0_RESET = 51          # M51 - reset output Y0
    Y1_TOGGLE = 52         # M52 - static level / power gate (Y1)
    Y2_FREQ_EN = 53        # M53 - frequency generator enable (Y2)
    Y2_FREQ_VAL = 90       # D90 - frequency setpoint (REAL)

    OUTPUT_BASE = Y0_SET   # read 4 output coils starting here
    INPUT_X0 = 100         # M100 - first of 4 input coils (X0..X3)
    DISCRETE_COUNT = 4

    #: Коилы, отражающие состояние индикаторов выходов Y0 / Y1 / Y2 (в порядке
    #: индикаторов на вкладке I/O).  Окно чтения M50..M53 содержит ещё M51
    #: (RST Y0), который индикатором не является, поэтому индекс в снимке
    #: ``Telemetry.outputs`` вычисляется как ``coil - OUTPUT_BASE``.
    OUTPUT_LED_COILS = (Y0_SET, Y1_TOGGLE, Y2_FREQ_EN)

    # --- Концевые выключатели всех осей -------------------------------- #
    # Все концевики (X1..X6) читаются одним сплошным блоком discrete inputs
    # (FC02), затем раскладываются по осям через AXIS_SAFETY.  Чтение
    # толерантное: если у ПЛК нет такой проекции входов, индикаторы гаснут,
    # но остальная телеметрия не страдает.  Окно вычисляется из AXIS_SAFETY,
    # чтобы карта адресов оставалась единственным источником истины.
    LIMIT_INPUT_BASE = min(
        min(s.limit_left, s.limit_right) for s in AXIS_SAFETY.values())
    LIMIT_INPUT_COUNT = max(
        max(s.limit_left, s.limit_right) for s in AXIS_SAFETY.values()
    ) - LIMIT_INPUT_BASE + 1


@dataclass
class GridLimits:
    """Display bounds for the XY plot and linear gauges (millimetres)."""

    x_min: float = -50.0
    x_max: float = 500.0
    y_min: float = -50.0
    y_max: float = 500.0
    z_min: float = -10.0
    z_max: float = 200.0


@dataclass
class AppSettings:
    """User-editable connection and display settings."""

    plc_ip: str = "192.168.1.88"
    plc_port: int = 502
    poll_interval_ms: int = 150

    scpi_ip: str = "192.168.1.99"
    scpi_port: int = 5025

    #: Establish the PLC link automatically on application startup.
    auto_connect: bool = False

    #: Axes whose servo drive is physically connected.  Scenario moves to any
    #: other axis are skipped (otherwise the run would wait forever for an
    #: arrival that never comes).
    active_axes: list = field(default_factory=lambda: [1, 2, 3])

    #: Position tolerance (mm) used to detect arrival during a scan sequence.
    arrival_tolerance_mm: float = 0.4

    #: Durable command queue (servo_core backbone) — SQLite file and policy.
    queue_db_path: str = str(_BASE_DIR / "scan_queue.db")
    #: Per-move completion slack before a retry / dead-letter (ms).  The
    #: runner adds the estimated travel time (distance / speed x margin) on
    #: top, so this only has to cover ramps, settling and link latency.
    move_timeout_ms: int = 30000
    #: Attempts (including the first) before a move goes to dead-letter.
    max_move_attempts: int = 3

    #: Directory for on-disk log files (created on startup).
    log_dir: str = str(_BASE_DIR / "log")

    limits: GridLimits = field(default_factory=GridLimits)

    # ------------------------------------------------------------------ #
    # Persistence (app_config.json)                                      #
    # ------------------------------------------------------------------ #
    def to_config_dict(self) -> dict:
        """The user-facing subset persisted to ``app_config.json``."""
        return {
            "plc_ip": self.plc_ip,
            "plc_port": self.plc_port,
            "poll_interval_ms": self.poll_interval_ms,
            "scpi_ip": self.scpi_ip,
            "scpi_port": self.scpi_port,
            "auto_connect": self.auto_connect,
            "active_axes": list(self.active_axes),
            "arrival_tolerance_mm": self.arrival_tolerance_mm,
            "move_timeout_ms": self.move_timeout_ms,
            "max_move_attempts": self.max_move_attempts,
            "limits": {
                "x_min": self.limits.x_min, "x_max": self.limits.x_max,
                "y_min": self.limits.y_min, "y_max": self.limits.y_max,
                "z_min": self.limits.z_min, "z_max": self.limits.z_max,
            },
        }

    def save(self, path: str | Path = CONFIG_PATH) -> None:
        """Write current settings to JSON (best-effort)."""
        try:
            Path(path).write_text(
                json.dumps(self.to_config_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass

    @classmethod
    def load(cls, path: str | Path = CONFIG_PATH) -> "AppSettings":
        """Build settings from ``app_config.json``; defaults fill any gaps.

        If the file is missing it is created with the defaults so the operator
        has something to edit.
        """
        s = cls()
        p = Path(path)
        if not p.exists():
            s.save(p)
            return s
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
        for name in ("plc_ip", "plc_port", "poll_interval_ms", "scpi_ip",
                     "scpi_port", "auto_connect", "active_axes",
                     "arrival_tolerance_mm", "queue_db_path", "move_timeout_ms",
                     "max_move_attempts", "log_dir"):
            if name in data:
                setattr(s, name, data[name])
        # Keep only valid, known axis numbers; fall back to all three.
        if isinstance(s.active_axes, list):
            s.active_axes = [int(a) for a in s.active_axes if int(a) in AXES]
        if not s.active_axes:
            s.active_axes = list(AXES)
        lim = data.get("limits")
        if isinstance(lim, dict):
            for key in ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max"):
                if key in lim:
                    setattr(s.limits, key, float(lim[key]))
        return s
