"""Handshake-автомат MotionExecutor против эталонного MockPlc."""
from __future__ import annotations

import threading
import time
from typing import Sequence

import pytest

from conftest import wait_until
from core.config import ChannelConfig, RegisterMap
from core.database import Database
from core.events import EventBus, EventType
from core.executor import MotionExecutor
from core.mock import MockModbusDriver, MockPlc
from core.models import MotionCommand
from core.protocol import PlcProtocol
from core.repository import CommandRepository

REGS = RegisterMap()


class SpyDriver(MockModbusDriver):
    """Mock-драйвер, запоминающий записи командного блока и потоки вызовов."""

    def __init__(self, plc: MockPlc):
        super().__init__(plc)
        self.command_writes: list[list[int]] = []
        self.threads: set[str] = set()

    def read_holding(self, address: int, count: int) -> list[int]:
        self.threads.add(threading.current_thread().name)
        return super().read_holding(address, count)

    def write_registers(self, address: int, values: Sequence[int]) -> None:
        self.threads.add(threading.current_thread().name)
        if address == REGS.cmd_block_start:
            self.command_writes.append(list(values))
        super().write_registers(address, values)


def _cfg(**kw) -> ChannelConfig:
    base = dict(name="CH", port="mock", registers=REGS,
                poll_interval_s=0.005, command_timeout_s=2.0)
    base.update(kw)
    return ChannelConfig(**base)


def _cmd(pos: int) -> MotionCommand:
    return MotionCommand(target_position=pos, speed=10, command_code=1)


@pytest.fixture
def repo(tmp_path):
    return CommandRepository(Database(tmp_path / "q.db"))


def _executor(cfg, driver, repo, events=None):
    driver.connect()
    return MotionExecutor(cfg, driver, PlcProtocol(REGS), repo,
                          events or EventBus())


def _statuses(repo):
    return [r["status"] for r in reversed(repo.history("CH"))]


def test_process_one_acks_by_plc_fact(repo):
    plc = MockPlc(REGS, move_time=0.05)
    events = EventBus()
    got = []
    events.subscribe(got.append)
    ex = _executor(_cfg(), MockModbusDriver(plc), repo, events)

    q = repo.enqueue("CH", _cmd(100))
    assert ex.process_one() is True
    assert plc._last_processed_seq == q.seq
    assert _statuses(repo) == ["DONE"]
    assert [e.type for e in got] == [EventType.SENT, EventType.DONE]
    assert ex.process_one() is False   # очередь пуста


def test_recovery_does_not_resend_already_done_command(repo):
    """Краш между FC16 и complete(): recovery закрывает без второго движения."""
    plc = MockPlc(REGS, move_time=0.05)
    repo.enqueue("CH", _cmd(100))
    claimed = repo.claim_next("CH")
    pre = MockModbusDriver(plc)
    pre.connect()
    pre.write_registers(REGS.cmd_block_start,
                        PlcProtocol(REGS).encode_command(claimed.command, claimed.seq))
    assert wait_until(lambda: plc._last_processed_seq == claimed.seq)

    spy = SpyDriver(plc)
    ex = _executor(_cfg(), spy, repo)
    ex.recover()

    assert spy.command_writes == []          # команда повторно не отправлялась
    assert repo.resume_in_flight("CH") == []
    assert _statuses(repo) == ["DONE"]


def test_recovery_sends_command_plc_never_saw(repo):
    plc = MockPlc(REGS, move_time=0.02)
    repo.enqueue("CH", _cmd(100))
    claimed = repo.claim_next("CH")          # краш до отправки

    spy = SpyDriver(plc)
    _executor(_cfg(), spy, repo).recover()

    assert len(spy.command_writes) == 1
    assert plc._last_processed_seq == claimed.seq
    assert _statuses(repo) == ["DONE"]


def test_plc_dedup_ignores_replayed_seq():
    plc = MockPlc(REGS, move_time=0.02)
    drv = MockModbusDriver(plc)
    drv.connect()
    block = PlcProtocol(REGS).encode_command(_cmd(5), 1)
    drv.write_registers(REGS.cmd_block_start, block)
    assert wait_until(lambda: plc._last_processed_seq == 1)
    drv.write_registers(REGS.cmd_block_start, block)   # повтор того же seq
    time.sleep(0.05)
    status = PlcProtocol(REGS).decode_status(
        drv.read_holding(REGS.status_block_start, REGS.status_block_length))
    assert status.ack_seq == 1 and status.state.value == "DONE"


def test_estop_sends_to_dead_letter_without_motion(repo):
    plc = MockPlc(REGS, move_time=0.02)
    plc.trigger_estop()
    spy = SpyDriver(plc)
    ex = _executor(_cfg(), spy, repo)
    repo.enqueue("CH", _cmd(100))
    ex.process_one()
    assert spy.command_writes == []
    [dl] = repo.dead_letters("CH")
    assert dl["reason"] == "SAFETY_LOCK"


def test_timeout_retries_then_dead_letters(repo):
    plc = MockPlc(REGS, move_time=5.0)      # ПЛК «не успевает»
    ex = _executor(_cfg(command_timeout_s=0.05, max_attempts=2),
                   MockModbusDriver(plc), repo)
    repo.enqueue("CH", _cmd(100))

    ex.process_one()                         # попытка 1 -> RETRY
    assert repo.pending_count("CH") == 1
    assert repo.dead_letters("CH") == []

    ex.process_one()                         # попытка 2 -> dead-letter
    [dl] = repo.dead_letters("CH")
    assert dl["reason"] == "TIMEOUT_LIMIT"
    assert repo.pending_count("CH") == 0


def test_transport_error_is_retryable(repo):
    plc = MockPlc(REGS)
    drv = MockModbusDriver(plc)              # не подключён -> ModbusError
    ex = MotionExecutor(_cfg(max_attempts=3), drv, PlcProtocol(REGS), repo,
                        EventBus())
    repo.enqueue("CH", _cmd(100))
    ex.process_one()
    assert repo.pending_count("CH") == 1
    assert repo.dead_letters("CH") == []
