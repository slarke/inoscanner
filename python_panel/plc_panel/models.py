"""Plain data structures shared between the PLC worker, runner and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


@dataclass
class AxisTelemetry:
    """Decoded telemetry for a single axis."""

    position: float = 0.0
    velocity: float = 0.0
    position_raw: List[int] = field(default_factory=lambda: [0, 0])
    velocity_raw: List[int] = field(default_factory=lambda: [0, 0])
    servo_error: int = 0
    axis_error: int = 0

    @property
    def has_error(self) -> bool:
        return self.servo_error != 0 or self.axis_error != 0


@dataclass
class Telemetry:
    """A single polling snapshot of the whole machine."""

    axes: Dict[int, AxisTelemetry] = field(default_factory=dict)
    block_error: int = 0
    inputs: List[bool] = field(default_factory=lambda: [False] * 4)
    outputs: List[bool] = field(default_factory=lambda: [False] * 4)
    #: Состояние силового контура (MC_Power) по осям: True=включён, False=выключен,
    #: None=неизвестно (коил не прочитан). Считывается с коилов M10/M20/M30.
    power: Dict[int, Optional[bool]] = field(default_factory=dict)
    #: Концевые выключатели (физические входы X1..X6): ключ = discrete-input
    #: адрес (1..6) -> True=замкнут, False=разомкнут, None=неизвестно
    #: (вход не прочитан).  Раскладка по осям — через config.AXIS_SAFETY.
    limit_inputs: Dict[int, Optional[bool]] = field(default_factory=dict)
    #: Признак заблокированного состояния по осям (катушки M100/M200/M300):
    #: ключ = номер оси -> True=заблокировано, False=норма, None=неизвестно.
    blocked: Dict[int, Optional[bool]] = field(default_factory=dict)


class StepType(str, Enum):
    """Kinds of step a scan sequence can contain.

    Values match the JSON used by the original app for import/export.
    """

    MOVE = "move"
    MOVE_PULSE = "move_pulse"
    DELAY = "delay"
    SCPI = "scpi"
    PULSE = "pulse"


@dataclass
class ScanStep:
    """One polymorphic step of an automatic scan scenario."""

    type: StepType
    axis: int = 1
    pos: float = 0.0
    spd: float = 10.0
    ms: int = 1000
    cmd: str = ":INITiate:IMMediate"

    @property
    def is_move(self) -> bool:
        return self.type in (StepType.MOVE, StepType.MOVE_PULSE)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to the JSON shape used by the original sequence files."""
        data: Dict[str, Any] = {"type": self.type.value}
        if self.is_move:
            data.update(axis=self.axis, pos=self.pos, spd=self.spd)
        elif self.type is StepType.DELAY:
            data["ms"] = self.ms
        elif self.type is StepType.SCPI:
            data["cmd"] = self.cmd
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["ScanStep"]:
        """Build a step from a JSON dict, or ``None`` if the type is unknown."""
        try:
            step_type = StepType(data["type"])
        except (KeyError, ValueError):
            return None

        step = cls(type=step_type)
        if step.is_move:
            step.axis = int(data.get("axis", 1))
            step.pos = float(data.get("pos", 0.0))
            step.spd = float(data.get("spd", 10.0))
        elif step_type is StepType.DELAY:
            step.ms = int(data.get("ms", 1000))
        elif step_type is StepType.SCPI:
            step.cmd = str(data.get("cmd", ""))
        return step
