"""Чистые функции панели: кодек REAL, ScanStep, маппинг очереди, настройки, индикаторы."""
from __future__ import annotations

import json

import pytest

from plc_panel.codec import float_to_words, floats_to_words, words_to_float
from plc_panel.config import IO, AppSettings
from plc_panel.models import ScanStep, StepType
from plc_panel.queue_backbone import MotionQueue, _to_command, decode_move


# --- codec ------------------------------------------------------------------
@pytest.mark.parametrize("value", [0.0, 1.0, -1.5, 123.456, -3000.0, 1e-3])
def test_real_roundtrip(value):
    assert words_to_float(float_to_words(value)) == pytest.approx(value, rel=1e-6)


def test_real_is_low_word_first():
    assert float_to_words(1.0) == [0x0000, 0x3F80]


def test_floats_to_words_concatenates():
    assert floats_to_words(1.0, 2.0) == float_to_words(1.0) + float_to_words(2.0)


def test_words_to_float_malformed():
    assert words_to_float([1]) == 0.0


# --- ScanStep ---------------------------------------------------------------
@pytest.mark.parametrize("step", [
    ScanStep(StepType.MOVE, axis=2, pos=12.5, spd=3.0),
    ScanStep(StepType.MOVE_PULSE, axis=3, pos=-1.0, spd=7.0),
    ScanStep(StepType.DELAY, ms=250),
    ScanStep(StepType.SCPI, cmd="*IDN?"),
    ScanStep(StepType.PULSE),
])
def test_scan_step_roundtrip(step):
    assert ScanStep.from_dict(step.to_dict()) == step


def test_scan_step_unknown_type():
    assert ScanStep.from_dict({"type": "teleport"}) is None
    assert ScanStep.from_dict({}) is None


# --- queue_backbone ---------------------------------------------------------
@pytest.mark.parametrize("stype,pulse", [(StepType.MOVE, False),
                                         (StepType.MOVE_PULSE, True)])
def test_step_command_roundtrip(stype, pulse):
    step = ScanStep(stype, axis=2, pos=123.456, spd=9.5)
    cmd = _to_command(step, "t")
    assert cmd.target_position == 123456
    from core.models import QueuedCommand, CommandState
    move = decode_move(QueuedCommand(1, 1, "SCAN", cmd, CommandState.PENDING, 0, 0.0))
    assert (move.axis, move.pos, move.spd, move.pulse) == (2, 123.456, 9.5, pulse)


def test_motion_queue_survives_restart(tmp_path):
    db = str(tmp_path / "scan.db")
    q1 = MotionQueue(db)
    q1.submit_move(ScanStep(StepType.MOVE, axis=1, pos=10.0))
    crashed = q1.claim_next()
    q2 = MotionQueue(db)
    [again] = q2.resume_in_flight()
    assert again.seq == crashed.seq
    q2.complete(again)
    assert q2.resume_in_flight() == [] and q2.history()[0]["status"] == "DONE"


# --- AppSettings ------------------------------------------------------------
def test_settings_created_when_missing(tmp_path):
    path = tmp_path / "app_config.json"
    s = AppSettings.load(path)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["plc_port"] == s.plc_port


def test_settings_filter_unknown_axes(tmp_path):
    path = tmp_path / "app_config.json"
    path.write_text(json.dumps({"active_axes": [2, 9], "move_timeout_ms": 1234,
                                "limits": {"x_max": 999}}), encoding="utf-8")
    s = AppSettings.load(path)
    assert s.active_axes == [2]
    assert s.move_timeout_ms == 1234
    assert s.limits.x_max == 999.0


def test_settings_empty_axes_fall_back_to_all(tmp_path):
    path = tmp_path / "app_config.json"
    path.write_text(json.dumps({"active_axes": [7]}), encoding="utf-8")
    assert AppSettings.load(path).active_axes == [1, 2, 3]


# --- индикаторы выходов (пункт 3) -------------------------------------------
def test_output_leds_map_to_their_own_coils():
    from plc_panel.ui.main_window import output_led_states

    # окно M50..M53 = [SET Y0, RST Y0, Y1, Y2_EN]
    assert IO.OUTPUT_LED_COILS == (IO.Y0_SET, IO.Y1_TOGGLE, IO.Y2_FREQ_EN)
    assert output_led_states([True, False, False, False]) == [True, False, False]
    # RST Y0 (M51) не должен зажигать индикатор Y1
    assert output_led_states([False, True, False, False]) == [False, False, False]
    assert output_led_states([False, False, True, False]) == [False, True, False]
    assert output_led_states([False, False, False, True]) == [False, False, True]


def test_output_leds_short_window_unknown():
    from plc_panel.ui.main_window import output_led_states

    assert output_led_states([True, False]) == [True, None, None]
