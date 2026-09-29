"""Supervisor/ChannelWorker: штатная работа, E-stop-гейт, acknowledge_safe_state."""
from __future__ import annotations

import threading
import time

import pytest

from conftest import wait_until
from core.config import ChannelConfig, RegisterMap
from core.database import Database
from core.events import EventBus, EventType
from core.mock import MockModbusDriver, MockPlc
from core.models import MotionCommand
from core.repository import CommandRepository
from core.service import CommandService
from core.supervisor import Supervisor

REGS = RegisterMap()
CH = "CH"


def _cfg() -> ChannelConfig:
    return ChannelConfig(name=CH, port="mock", registers=REGS,
                         poll_interval_s=0.005, command_timeout_s=2.0,
                         reconnect_backoff_s=0.05)


def _cmd(pos: int) -> MotionCommand:
    return MotionCommand(target_position=pos, speed=10, command_code=1)


@pytest.fixture
def stack(tmp_path):
    repo = CommandRepository(Database(tmp_path / "q.db"))
    events = EventBus()
    got = []
    events.subscribe(got.append)
    sup = Supervisor(repo, events)
    yield repo, events, got, sup
    sup.stop()


def _types(got):
    return [e.type for e in got]


def _spy_recover(sup):
    """Подменить executor.recover, запоминая имя потока каждого вызова."""
    worker = sup._workers[CH]
    calls: list[str] = []
    original = worker._executor.recover

    def recover():
        calls.append(threading.current_thread().name)
        original()

    worker._executor.recover = recover
    return worker, calls


def test_commands_flow_to_done(stack):
    repo, events, _got, sup = stack
    plc = MockPlc(REGS, move_time=0.02)
    sup.add_channel(_cfg(), MockModbusDriver(plc))
    sup.start()
    svc = CommandService(repo, events)
    seqs = [svc.submit(CH, _cmd(100 * i)).seq for i in range(1, 4)]

    assert wait_until(lambda: plc._last_processed_seq == seqs[-1])
    assert wait_until(lambda: all(r["status"] == "DONE" for r in repo.history(CH)))


def test_estop_blocks_recovery_until_acknowledged_in_worker_thread(stack):
    repo, _events, got, sup = stack
    plc = MockPlc(REGS, move_time=0.02)
    plc.trigger_estop()
    repo.enqueue(CH, _cmd(100))
    claimed = repo.claim_next(CH)            # «краш» с IN_FLIGHT-командой

    sup.add_channel(_cfg(), MockModbusDriver(plc))
    worker, calls = _spy_recover(sup)
    sup.start()

    assert wait_until(lambda: EventType.ESTOP in _types(got))
    time.sleep(0.1)
    assert calls == []                        # recovery не запускался
    assert [q.seq for q in repo.resume_in_flight(CH)] == [claimed.seq]

    plc.clear_estop()
    t0 = time.monotonic()
    sup.acknowledge_safe_state(CH)            # вызов из «GUI»-потока
    assert time.monotonic() - t0 < 0.05       # не блокирует вызывающего

    assert wait_until(lambda: repo.resume_in_flight(CH) == [])
    assert calls == [f"worker-{CH}"]          # recovery — в потоке канала
    assert plc._last_processed_seq == claimed.seq


def test_stale_acknowledge_does_not_unblock_fresh_estop(stack):
    repo, _events, got, sup = stack
    plc = MockPlc(REGS, move_time=0.02)
    plc.trigger_estop()
    repo.enqueue(CH, _cmd(100))
    repo.claim_next(CH)

    sup.add_channel(_cfg(), MockModbusDriver(plc))
    worker, calls = _spy_recover(sup)
    worker.acknowledge_safe_state()           # до подключения/проверки гейта
    sup.start()

    assert wait_until(lambda: EventType.ESTOP in _types(got))
    time.sleep(0.15)
    assert calls == []
    assert len(repo.resume_in_flight(CH)) == 1


def test_new_commands_wait_while_blocked(stack):
    repo, events, got, sup = stack
    plc = MockPlc(REGS, move_time=0.02)
    plc.trigger_estop()
    sup.add_channel(_cfg(), MockModbusDriver(plc))
    sup.start()
    assert wait_until(lambda: EventType.ESTOP in _types(got))

    CommandService(repo, events).submit(CH, _cmd(100))
    time.sleep(0.1)
    assert repo.pending_count(CH) == 1        # канал заблокирован — не берёт

    plc.clear_estop()
    sup.acknowledge_safe_state(CH)
    assert wait_until(lambda: repo.pending_count(CH) == 0
                      and repo.resume_in_flight(CH) == [])
    assert repo.history(CH)[0]["status"] == "DONE"
