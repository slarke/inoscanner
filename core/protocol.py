"""Протокольный слой: перевод между доменом (команда/seq) и регистрами.

Отделён и от транспорта (ModbusDriver), и от оркестрации (MotionExecutor):
здесь только кодирование/декодирование по карте RegisterMap.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import FLAG_ESTOP, FLAG_GUARD_OPEN, FLAG_READY, RegisterMap
from .models import PLC_STATE_BY_CODE, MotionCommand, PlcCommandState


def u32_to_regs(value: int) -> tuple[int, int]:
    value &= 0xFFFFFFFF
    return (value >> 16) & 0xFFFF, value & 0xFFFF


def regs_to_u32(hi: int, lo: int) -> int:
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)


@dataclass(frozen=True)
class PlcStatus:
    """Декодированный статус-блок ПЛК."""

    ack_seq: int
    state: PlcCommandState
    error_code: int
    flags: int

    @property
    def ready(self) -> bool:
        return bool(self.flags & FLAG_READY)

    @property
    def estop(self) -> bool:
        return bool(self.flags & FLAG_ESTOP)

    @property
    def guard_open(self) -> bool:
        return bool(self.flags & FLAG_GUARD_OPEN)

    @property
    def safe_to_move(self) -> bool:
        return self.ready and not self.estop and not self.guard_open


class PlcProtocol:
    def __init__(self, registers: RegisterMap):
        self._r = registers

    def encode_command(self, command: MotionCommand, seq: int) -> list[int]:
        """[target_hi, target_lo, speed, command_code, seq_hi, seq_lo]."""
        pos_hi, pos_lo = u32_to_regs(command.target_position & 0xFFFFFFFF)
        seq_hi, seq_lo = u32_to_regs(seq)
        return [
            pos_hi, pos_lo,
            command.speed & 0xFFFF,
            command.command_code & 0xFFFF,
            seq_hi, seq_lo,
        ]

    def decode_status(self, regs: list[int]) -> PlcStatus:
        ack = regs_to_u32(regs[0], regs[1])
        state = PLC_STATE_BY_CODE.get(regs[2], PlcCommandState.UNKNOWN)
        return PlcStatus(
            ack_seq=ack, state=state, error_code=regs[3], flags=regs[4]
        )
