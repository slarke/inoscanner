"""PlcController на эмуляторе текущего ладдера (без HTTP)."""
from __future__ import annotations

import time

import pytest

from conftest import wait_until
from plc_api.controller import (
    ApiError,
    Conflict,
    ControllerConfig,
    NotConnected,
    NotFound,
    OutOfLimits,
    PlcController,
    Unsupported,
    WaitTimeout,
)
from plc_api.sim import SimulatedPlc
from plc_panel.config import AXES, AXIS_SAFETY, IO, GridLimits


def _cfg(**kw) -> ControllerConfig:
    base = dict(poll_interval_s=0.02, reconnect_interval_s=0.05,
                command_timeout_s=3.0, arrival_tolerance_mm=0.05,
                active_axes=(1, 2, 3),
                limits=GridLimits(x_min=-10, x_max=500, y_min=-10, y_max=500,
                                  z_min=-10, z_max=200))
    base.update(kw)
    return ControllerConfig(**base)


@pytest.fixture
def plc():
    return SimulatedPlc()


@pytest.fixture
def ctl(plc):
    c = PlcController(plc, _cfg())
    events: list[dict] = []
    c.subscribe(events.append)
    c.events = events          # для проверок в тестах
    c.start()
    assert wait_until(lambda: c.telemetry() is not None)
    yield c
    c.shutdown()


def _types(ctl):
    return [e["type"] for e in ctl.events]


def _pos(ctl, name):
    return ctl.telemetry()["axes"][name]["position"]


def test_connects_and_publishes_telemetry(ctl, plc):
    snap = ctl.telemetry()
    assert set(snap["axes"]) == {"X", "Y", "Z"}
    assert snap["errors"]["shared_between_axes"] is True
    assert ctl.events[0] == {"type": "connection", "connected": True,
                             "detail": "подключено"}
    # MC_ReadAxisError включены на подключении, как в PlcWorker
    assert all(plc.coil(r.read_error_en) for r in AXES.values())


def test_move_absolute_arrives_by_telemetry(ctl, plc):
    ctl.power("x", True)
    result = ctl.move_absolute("x", 25.0, 500.0)
    state = ctl.wait_arrival("x", 25.0, result["issued_at_mono"], timeout_s=3)
    assert state["position"] == pytest.approx(25.0, abs=0.05)
    assert wait_until(lambda: "arrived" in _types(ctl))
    arrived = next(e for e in ctl.events if e["type"] == "arrived")
    assert arrived["axis"] == "X" and arrived["target"] == 25.0


def test_move_without_power_is_ignored_by_ladder(ctl):
    result = ctl.move_absolute("y", 10.0, 500.0)
    with pytest.raises(WaitTimeout):
        ctl.wait_arrival("y", 10.0, result["issued_at_mono"], timeout_s=0.3)


def test_pulse_only_on_x(ctl, plc):
    ctl.power(1, True)
    with pytest.raises(Unsupported):
        ctl.move_absolute("y", 10.0, 100.0, pulse=True)
    with pytest.raises(Unsupported):
        ctl.move_absolute("z", 10.0, 100.0, pulse=True)
    r = ctl.move_absolute("x", 5.0, 500.0, pulse=True)
    ctl.wait_arrival("x", 5.0, r["issued_at_mono"], timeout_s=3)
    assert wait_until(lambda: plc.y0_pulses == 1)


def test_validation(ctl):
    with pytest.raises(NotFound):
        ctl.move_absolute("w", 1.0, 1.0)
    with pytest.raises(ApiError):
        ctl.move_absolute("x", 1.0, 0.0)            # скорость <= 0
    with pytest.raises(ApiError):
        ctl.move_absolute("x", float("nan"), 1.0)
    with pytest.raises(ApiError):
        ctl.move_velocity("x", 0.0, 1.0, 1.0)
    with pytest.raises(OutOfLimits):
        ctl.move_absolute("z", 250.0, 10.0)          # z_max = 200


def test_relative_move_checks_limits_from_current_position(ctl):
    ctl.power("x", True)
    r = ctl.move_absolute("x", 480.0, 5000.0)
    ctl.wait_arrival("x", 480.0, r["issued_at_mono"], timeout_s=3)
    with pytest.raises(OutOfLimits):
        ctl.move_relative("x", 30.0, 10.0)            # 510 > x_max
    ctl.move_relative("x", -30.0, 5000.0)
    assert wait_until(lambda: abs(_pos(ctl, "X") - 450.0) < 0.05)


def test_inactive_axis_rejected(plc):
    c = PlcController(plc, _cfg(active_axes=(1,)))
    c.start()
    try:
        assert wait_until(lambda: c.connected)
        with pytest.raises(Conflict):
            c.move_absolute("y", 1.0, 1.0)
    finally:
        c.shutdown()


def test_limit_switch_blocks_until_unblock(ctl, plc):
    s = AXIS_SAFETY[1]
    ctl.power("x", True)
    plc.set_input(s.limit_left, True)                 # концевик X1
    assert wait_until(lambda: ctl.telemetry()["axes"]["X"]["blocked"] is True)
    assert {"type": "blocked", "axis": "X", "blocked": True} in ctl.events
    with pytest.raises(Conflict):
        ctl.move_absolute("x", 10.0, 100.0)

    ctl.unblock("x")
    assert wait_until(lambda: ctl.telemetry()["axes"]["X"]["blocked"] is False)
    r = ctl.move_absolute("x", 10.0, 500.0)
    ctl.wait_arrival("x", 10.0, r["issued_at_mono"], timeout_s=3)


def test_block_during_wait_raises_conflict(ctl, plc):
    ctl.power("x", True)
    r = ctl.move_absolute("x", 400.0, 50.0)           # ~8 с хода
    time.sleep(0.1)
    plc.set_input(AXIS_SAFETY[1].limit_right, True)
    with pytest.raises(Conflict):
        ctl.wait_arrival("x", 400.0, r["issued_at_mono"], timeout_s=3)


def test_stop_aborts_arrival_wait_immediately(ctl):
    import threading
    from plc_api.controller import MotionAborted

    ctl.power("x", True)
    r = ctl.move_absolute("x", 400.0, 20.0)           # ~20 с хода
    outcome = {}

    def waiter():
        t0 = time.monotonic()
        try:
            ctl.wait_arrival("x", 400.0, r["issued_at_mono"], 30.0, r["motion_gen"])
        except MotionAborted as exc:
            outcome["error"] = exc
        outcome["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.1)
    ctl.stop("x")
    th.join(timeout=3)
    assert isinstance(outcome.get("error"), MotionAborted)
    assert outcome["elapsed"] < 1.0                   # а не 30 с таймаута


def test_other_axis_command_does_not_abort_wait(ctl):
    ctl.power("x", True)
    r = ctl.move_absolute("x", 5.0, 50.0)
    ctl.stop("y")                                     # другая ось
    state = ctl.wait_arrival("x", 5.0, r["issued_at_mono"], 3.0, r["motion_gen"])
    assert state["position"] == pytest.approx(5.0, abs=0.05)


def test_stop_halts_motion(ctl):
    ctl.power("x", True)
    ctl.move_velocity("x", 100.0, 10.0, 10.0)
    assert wait_until(lambda: _pos(ctl, "X") > 1.0)
    ctl.stop("x")
    time.sleep(0.1)
    p1 = _pos(ctl, "X")
    time.sleep(0.2)
    assert _pos(ctl, "X") == pytest.approx(p1)


def test_stop_all_with_power_off(ctl, plc):
    for a in ("x", "y", "z"):
        ctl.power(a, True)
    ctl.move_velocity("y", 50.0, 10.0, 10.0)
    ctl.stop_all(power_off=True)
    assert not any(plc.coil(r.power) for r in AXES.values())
    assert not any(plc.coil(r.stop_exec) for r in AXES.values())


def test_y0_low_does_not_hold_reset_coil(ctl, plc):
    ctl.y0_set(True)
    assert plc.coil(IO.Y0_SET) and plc.y0_level
    ctl.y0_set(False)
    assert not plc.coil(IO.Y0_SET)
    assert not plc.coil(IO.Y0_RESET)                  # M51 не удерживается
    assert plc.y0_level is False


def test_link_loss_rejects_commands_and_reconnects(plc):
    c = PlcController(plc, _cfg(reconnect_interval_s=0.5))
    events: list[dict] = []
    c.subscribe(events.append)
    c.start()
    try:
        assert wait_until(lambda: c.telemetry() is not None)
        plc.drop_link()
        assert wait_until(lambda: not c.connected)
        assert c.telemetry() is None                  # старый кадр не отдаём
        with pytest.raises(NotConnected):
            c.power("x", True)
        assert wait_until(lambda: c.connected, timeout=3)   # пауза, затем повтор
        assert wait_until(lambda: c.telemetry() is not None)
        conn = [e["connected"] for e in events if e["type"] == "connection"]
        assert conn == [True, False, True]
    finally:
        c.shutdown()


def test_transient_read_failures_do_not_drop_link(ctl, plc):
    plc.read_failures = 2                             # < poll_failure_limit
    time.sleep(0.2)
    assert ctl.connected
    assert [e for e in ctl.events if e["type"] == "connection"][1:] == []


def test_initial_connect_retries(plc):
    plc.connect_failures = 2
    c = PlcController(plc, _cfg())
    c.start()
    try:
        assert wait_until(lambda: c.connected, timeout=3)
    finally:
        c.shutdown()
