"""ScenarioRunner: таймаут хода по оценке времени (п.4) и взаимоисключение
recovery с запуском сценария (п.5). Реальный PLC заменён фиктивным сервисом,
телеметрия подаётся вручную."""
from __future__ import annotations

import time

import pytest
from PyQt5.QtCore import QCoreApplication, QObject, pyqtSignal

from plc_panel.config import AppSettings
from plc_panel.models import AxisTelemetry, ScanStep, StepType, Telemetry
from plc_panel.queue_backbone import MotionQueue, Move
from plc_panel.scenario import _TRAVEL_MARGIN, ScenarioRunner


class FakeService(QObject):
    """Двойник PlcService: записывает команды, телеметрию шлёт тест."""

    telemetry = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.moves: list[tuple] = []
        self.power: list[tuple] = []

    def move_absolute(self, axis, pos, spd, pulse):
        self.moves.append((axis, pos))

    def set_power(self, axis, on):
        self.power.append((axis, on))

    def send_scpi(self, *_a):
        pass

    def pulse_trigger(self):
        pass


def pump(ms: int) -> None:
    """Прокрутить цикл событий Qt ms миллисекунд (таймеры раннера)."""
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def pump_until(cond, timeout_ms: int = 3000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def tele(estop: bool = False, **positions: float) -> Telemetry:
    """tele(x=0, y=10, estop=True) -> кадр телеметрии."""
    idx = {"x": 1, "y": 2, "z": 3}
    return Telemetry(
        axes={idx[k]: AxisTelemetry(position=v) for k, v in positions.items()},
        inputs=[False, False, False, estop])


def move(axis: int, pos: float, spd: float = 10.0) -> ScanStep:
    return ScanStep(StepType.MOVE, axis=axis, pos=pos, spd=spd)


@pytest.fixture
def env(qapp, tmp_path):
    def make(**settings_kw):
        settings = AppSettings(queue_db_path=str(tmp_path / "scan.db"),
                               **settings_kw)
        queue = MotionQueue(settings.queue_db_path)
        service = FakeService()
        runner = ScenarioRunner(service, settings, queue)
        logs: list[str] = []
        runner.log.connect(logs.append)
        return runner, service, queue, logs
    return make


def crash_with_in_flight(queue: MotionQueue, step: ScanStep):
    """Оставить в БД IN_FLIGHT-ход, как после краша прошлого запуска."""
    queue.submit_move(step)
    return queue.claim_next()


# --------------------------------------------------------------------------- #
# п.4 — таймаут хода                                                          #
# --------------------------------------------------------------------------- #
def test_timeout_adds_estimated_travel_time(env):
    runner, service, _q, _ = env(move_timeout_ms=3000)
    service.telemetry.emit(tele(x=0.0))
    expected = 3000 + int(100 / 10 * 1000 * _TRAVEL_MARGIN)
    assert runner._move_timeout_ms(Move(1, 100.0, 10.0, False)) == expected


def test_timeout_falls_back_to_base(env):
    runner, service, _q, _ = env(move_timeout_ms=3000)
    assert runner._move_timeout_ms(Move(1, 100.0, 10.0, False)) == 3000  # нет кадра
    service.telemetry.emit(tele(x=0.0))
    assert runner._move_timeout_ms(Move(1, 100.0, 0.0, False)) == 3000   # spd=0
    assert runner._move_timeout_ms(Move(2, 100.0, 10.0, False)) == 3000  # нет оси


def test_long_move_is_not_dead_lettered_early(env):
    """Раньше: 3 c × 3 попытки -> dead-letter хода на 10 c. Теперь — ждём."""
    runner, service, queue, _ = env(move_timeout_ms=100, max_move_attempts=1)
    service.telemetry.emit(tele(x=0.0))
    runner.start([move(1, 5.0, spd=10.0)])        # ~500 мс хода
    assert pump_until(lambda: service.moves == [(1, 5.0)])
    assert runner._timeout.interval() == 100 + int(500 * _TRAVEL_MARGIN)

    pump(400)                                       # дольше базового таймаута
    assert runner.is_active and queue.dead_letters() == []
    service.telemetry.emit(tele(x=5.0))             # ось приехала
    assert not runner.is_active
    assert queue.history()[0]["status"] == "DONE"


def test_stuck_move_still_retries_then_dead_letters(env):
    runner, service, queue, _ = env(move_timeout_ms=50, max_move_attempts=2)
    finished = []
    runner.finished.connect(lambda: finished.append(True))
    service.telemetry.emit(tele(x=0.0))
    runner.start([move(1, 1.0, spd=100.0)])
    assert pump_until(lambda: bool(finished), timeout_ms=3000)
    assert service.moves == [(1, 1.0), (1, 1.0)]   # первая попытка + повтор
    [dl] = queue.dead_letters()
    assert dl["reason"] == "TIMEOUT_LIMIT"
    assert not runner.is_active


# --------------------------------------------------------------------------- #
# п.5 — recovery и запуск сценария не пересекаются                            #
# --------------------------------------------------------------------------- #
def test_start_is_deferred_until_recovery_finishes(env):
    runner, service, queue, _ = env()
    crashed = crash_with_in_flight(queue, move(1, 50.0))

    runner.recover()                                 # on connected
    runner.start([move(2, 20.0)])                    # resume сессии
    assert not runner.is_active and service.power == []

    service.telemetry.emit(tele(x=0.0, y=0.0))       # ось X не в точке
    assert service.moves == [(1, 50.0)]              # только recovery-ход
    assert runner._inflight.seq == crashed.seq
    assert not runner.is_active

    service.telemetry.emit(tele(x=50.0, y=0.0))      # recovery-ход завершён
    assert runner.is_active                          # отложенный старт
    assert pump_until(lambda: len(service.moves) == 2)
    assert service.moves[1] == (2, 20.0)
    assert runner._inflight.seq != crashed.seq

    service.telemetry.emit(tele(x=50.0, y=20.0))
    assert not runner.is_active
    assert queue.resume_in_flight() == []
    assert {r["status"] for r in queue.history()} == {"DONE"}


def test_recovery_at_target_starts_scenario_immediately(env):
    runner, service, queue, _ = env()
    crash_with_in_flight(queue, move(1, 50.0))
    runner.recover()
    runner.start([move(2, 20.0)])
    service.telemetry.emit(tele(x=50.0, y=0.0))     # уже в точке -> без движения
    assert runner.is_active
    assert pump_until(lambda: service.moves == [(2, 20.0)])


def test_estop_blocks_recovery_and_deferred_start(env):
    runner, service, queue, _ = env()
    crash_with_in_flight(queue, move(1, 50.0))
    runner.recover()
    runner.start([move(2, 20.0)])

    service.telemetry.emit(tele(estop=True, x=0.0, y=0.0))
    pump(50)
    assert service.moves == [] and not runner.is_active

    runner.acknowledge_safe_state()
    assert service.moves == [(1, 50.0)]
    service.telemetry.emit(tele(x=50.0, y=0.0))
    assert runner.is_active


def test_acknowledgement_is_not_reused_by_next_recovery(env):
    runner, service, queue, _ = env()
    crash_with_in_flight(queue, move(1, 50.0))
    runner.recover()
    service.telemetry.emit(tele(estop=True, x=0.0))
    runner.acknowledge_safe_state()
    service.telemetry.emit(tele(x=50.0))            # recovery завершён

    service.telemetry.emit(tele(estop=True, x=50.0))  # снова E-stop
    crash_with_in_flight(queue, move(1, 60.0))      # новый «краш»
    runner.recover()
    service.telemetry.emit(tele(estop=True, x=50.0))
    assert service.moves == [(1, 50.0)]             # снова ждём оператора


def test_stop_cancels_recovery_and_deferred_start(env):
    runner, service, queue, _ = env()
    crashed = crash_with_in_flight(queue, move(1, 50.0))
    runner.recover()
    runner.start([move(2, 20.0)])
    runner.stop()

    service.telemetry.emit(tele(x=0.0, y=0.0))
    pump(50)
    assert service.moves == [] and not runner.is_active
    # незавершённый ход остаётся для следующего подключения
    assert [q.seq for q in queue.resume_in_flight()] == [crashed.seq]


def test_stop_during_recovery_move_prevents_reissue(env):
    runner, service, queue, _ = env(move_timeout_ms=50)
    crash_with_in_flight(queue, move(1, 50.0))
    runner.recover()
    service.telemetry.emit(tele(x=0.0))
    assert service.moves == [(1, 50.0)]
    runner.stop()                                   # АВАРИЙНЫЙ СТОП
    pump(200)                                       # таймаут не должен сработать
    assert service.moves == [(1, 50.0)]


def test_failed_recovery_cancels_deferred_start(env):
    runner, service, queue, logs = env(move_timeout_ms=30, max_move_attempts=1)
    crash_with_in_flight(queue, move(1, 1.0))
    runner.recover()
    runner.start([move(2, 20.0)])
    service.telemetry.emit(tele(x=0.0, y=0.0))

    assert pump_until(lambda: queue.dead_letters() != [])
    pump(700)
    assert not runner.is_active
    assert service.moves == [(1, 1.0)]              # сценарий не стартовал
    assert any("отложенный запуск" in line for line in logs)


def test_repeated_recover_does_not_duplicate(env):
    runner, service, queue, _ = env()
    crash_with_in_flight(queue, move(1, 50.0))
    runner.recover()
    runner.recover()                                # повторный connected
    service.telemetry.emit(tele(x=0.0))
    service.telemetry.emit(tele(x=1.0))
    assert service.moves == [(1, 50.0)]
