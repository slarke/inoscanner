"""Доменные модели ядра: состояния и команды.

Здесь нет ни SQL, ни Modbus, ни Qt — только предметная область.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CommandState(str, Enum):
    """Состояние команды в очереди (сторона Python)."""

    PENDING = "PENDING"        # лежит в очереди, исполнителем не взята
    IN_FLIGHT = "IN_FLIGHT"    # взята, отправлена/отправляется на ПЛК
    DONE = "DONE"              # ПЛК подтвердил выполнение
    FAILED = "FAILED"          # ушла в dead-letter


class PlcCommandState(str, Enum):
    """Зеркало автомата состояний на стороне ПЛК (читается из регистра)."""

    IDLE = "IDLE"
    RECEIVED = "RECEIVED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    ERROR = "ERROR"
    ABORTED = "ABORTED"
    UNKNOWN = "UNKNOWN"


# Числовая кодировка регистра command_state на ПЛК.
PLC_STATE_BY_CODE: dict[int, PlcCommandState] = {
    0: PlcCommandState.IDLE,
    1: PlcCommandState.RECEIVED,
    2: PlcCommandState.RUNNING,
    3: PlcCommandState.DONE,
    4: PlcCommandState.ERROR,
    5: PlcCommandState.ABORTED,
}
PLC_CODE_BY_STATE: dict[PlcCommandState, int] = {
    v: k for k, v in PLC_STATE_BY_CODE.items()
}


class DeadLetterReason(str, Enum):
    """Таксономия причин попадания в dead-letter — упрощает диагностику."""

    PLC_ERROR = "PLC_ERROR"
    TIMEOUT_LIMIT = "TIMEOUT_LIMIT"
    INVALID_COMMAND = "INVALID_COMMAND"
    SAFETY_LOCK = "SAFETY_LOCK"


@dataclass(frozen=True)
class MotionCommand:
    """Полезная нагрузка одной команды движения.

    target_position — АБСОЛЮТНАЯ уставка. Абсолютные команды идемпотентны:
    повторная отправка не накапливает смещение (в отличие от относительных).
    """

    target_position: int
    speed: int
    command_code: int
    operator: str = "system"

    def to_payload(self) -> dict:
        return {
            "target_position": self.target_position,
            "speed": self.speed,
            "command_code": self.command_code,
            "operator": self.operator,
        }

    @staticmethod
    def from_payload(data: dict) -> "MotionCommand":
        return MotionCommand(
            target_position=int(data["target_position"]),
            speed=int(data["speed"]),
            command_code=int(data["command_code"]),
            operator=data.get("operator", "system"),
        )


@dataclass(frozen=True)
class QueuedCommand:
    """Команда как она лежит в очереди БД: payload + служебные поля."""

    id: int
    seq: int
    channel: str
    command: MotionCommand
    state: CommandState
    attempts: int
    created_at: float
