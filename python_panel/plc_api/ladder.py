"""Контракт текущей программы ПЛК (MAIN.LD) с точки зрения API.

Адреса берутся из :mod:`plc_panel.config` (единственный источник истины для
Python). Здесь зафиксировано, *что из этих адресов ладдер реально исполняет*,
чтобы API не обещал того, чего ПЛК не сделает:

* импульс замера по приезду (M14 → TPR → Y0) подключён только у оси X:
  у Y выход таймера ``out_pulse_y`` никуда не выведен, у Z таймера нет;
* MC_Stop берёт замедление из переменных ПЛК, а не из D50/D116/D146 —
  параметр замедления в API отсутствует;
* все три MC_ReadAxisError пишут в общий блок D220/D222;
* M10/M20/M30 — команда MC_Power, а не фактический статус привода;
* выходов Y1 (M52), Y2 (M53/D90), входа E-stop и handshake-регистров D400+
  в программе нет;
* адреса концевиков X1…X6 как discrete inputs 1…6 не подтверждены.

При изменении ладдера правится этот модуль, а не контроллер/сервер.
"""
from __future__ import annotations

from typing import Dict, Optional

from plc_panel.config import AXES, AXIS_SAFETY, IO, AxisRegisters, AxisSafety

#: Оси, у которых ладдер выдаёт импульс замера по Done MoveAbsolute.
PULSE_AXES = frozenset({1})

#: Общий блок ошибок MC_ReadAxisError: [ServoErrorID, -, AxisErrorID].
SHARED_ERROR_BLOCK = AXES[1].error_block
SHARED_ERROR_COUNT = 3

#: Длительности стробов и пауз (мс) — как в plc_panel.modbus_worker.
STROBE_MS = 40
RESET_STROBE_MS = 100
PARAM_SETTLE_MS = 20
VELOCITY_SETTLE_MS = 50
PULSE_ENABLE_SETTLE_MS = 10
Y0_STROBE_MS = 50
Y0_GAP_MS = 150

#: Имена осей для URL/JSON и обратное отображение.
AXIS_NAMES: Dict[int, str] = {n: r.name for n, r in AXES.items()}
_AXIS_BY_TOKEN: Dict[str, int] = {
    **{r.name.lower(): n for n, r in AXES.items()},
    **{str(n): n for n in AXES},
}


def parse_axis(token: str) -> Optional[int]:
    """'x' / 'X' / '1' -> 1; неизвестная ось -> None."""
    return _AXIS_BY_TOKEN.get(str(token).strip().lower())


def axis_registers(axis: int) -> AxisRegisters:
    return AXES[axis]


def axis_safety(axis: int) -> AxisSafety:
    return AXIS_SAFETY[axis]


def capabilities() -> dict:
    """Машиночитаемое описание возможностей текущего ладдера (для /capabilities)."""
    axes = {}
    for n, r in AXES.items():
        s = AXIS_SAFETY[n]
        axes[r.name] = {
            "axis": n,
            "coils": {
                "power": r.power, "reset": r.reset, "move_absolute": r.abs_exec,
                "move_velocity": r.vel_exec, "move_relative": r.rel_exec,
                "stop": r.stop_exec, "set_position": r.set_pos_exec,
                "block_latch": s.block_latch, "block_reset": s.block_reset,
            },
            "registers": {
                "move_absolute": [r.abs_pos, r.abs_spd],
                "move_relative": [r.rel_distance, r.rel_speed],
                "move_velocity": [r.vel_block, r.vel_block + 2, r.vel_block + 4],
                "set_position": r.set_pos_val,
                "actual_position": r.act_pos,
                "actual_velocity": r.act_vel,
            },
            "limit_inputs": [s.limit_left, s.limit_right],
            "pulse_on_arrival": n in PULSE_AXES,
        }
    return {
        "source": "MAIN.LD (текущая версия в корне репозитория)",
        "real_format": "IEEE-754 float32, младшее слово первым",
        "axes": axes,
        "y0": {"set": IO.Y0_SET, "reset": IO.Y0_RESET},
        "errors": {
            "block_error_id": IO.BLOCK_ERROR_ID,
            "shared_axis_error_block": SHARED_ERROR_BLOCK,
            "note": "ServoErrorID/AxisErrorID всех осей пишутся в общий "
                    "D220/D222 — значение принадлежит оси, чей блок выполнился "
                    "последним",
        },
        "limitations": [
            "импульс замера по приезду только на оси X",
            "замедление MC_Stop задаётся переменными ПЛК и из API не меняется",
            "power_command — команда M10/M20/M30, а не фактический статус привода",
            "выходы Y1 (M52) и Y2 (M53/D90) в ладдере не реализованы",
            "входа аварийного стопа в ладдере нет; /stop-all — программный стоп",
            "адреса концевиков X1..X6 (discrete inputs 1..6) не подтверждены",
        ],
    }
