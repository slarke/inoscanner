"""Кодирование команды/статуса по карте регистров (core.protocol)."""
from __future__ import annotations

import pytest

from core.config import FLAG_ESTOP, FLAG_GUARD_OPEN, FLAG_READY, RegisterMap
from core.models import MotionCommand, PlcCommandState
from core.protocol import PlcProtocol, regs_to_u32, u32_to_regs


@pytest.mark.parametrize("value", [0, 1, 0xFFFF, 0x10000, 0x12345678, 0xFFFFFFFF])
def test_u32_roundtrip(value):
    assert regs_to_u32(*u32_to_regs(value)) == value


def test_u32_is_high_word_first():
    assert u32_to_regs(0x12345678) == (0x1234, 0x5678)


def test_encode_command_layout_seq_last():
    proto = PlcProtocol(RegisterMap())
    cmd = MotionCommand(target_position=0x00010002, speed=500, command_code=7)
    assert proto.encode_command(cmd, seq=0x00030004) == [1, 2, 500, 7, 3, 4]


def test_encode_negative_position_as_twos_complement():
    proto = PlcProtocol(RegisterMap())
    cmd = MotionCommand(target_position=-1, speed=1, command_code=1)
    assert proto.encode_command(cmd, 1)[:2] == [0xFFFF, 0xFFFF]


def test_decode_status_and_flags():
    proto = PlcProtocol(RegisterMap())
    st = proto.decode_status([0, 42, 3, 0, FLAG_READY])
    assert st.ack_seq == 42
    assert st.state is PlcCommandState.DONE
    assert st.ready and not st.estop and st.safe_to_move

    st = proto.decode_status([0, 0, 0, 0, FLAG_READY | FLAG_ESTOP])
    assert st.estop and not st.safe_to_move
    st = proto.decode_status([0, 0, 0, 0, FLAG_READY | FLAG_GUARD_OPEN])
    assert st.guard_open and not st.safe_to_move


def test_decode_unknown_state_code():
    st = PlcProtocol(RegisterMap()).decode_status([0, 0, 99, 0, 0])
    assert st.state is PlcCommandState.UNKNOWN
