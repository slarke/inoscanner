"""Durable command-queue backbone for the scan scenario.

Reuses **servo_core** (the ``core/`` package) as the persistence backbone:
its single-file WAL SQLite, transactional ``queue + history + dead-letter``,
monotonic per-channel ``seq`` and crash recovery.  What it deliberately does
*not* reuse is servo_core's PLC *handshake* protocol — that assumes a PLC that
mirrors ``ack_seq`` / ``command_state`` registers, which ``MAIN.LD`` does not
implement.  Execution therefore stays on the real :class:`PlcWorker` (strobe
coils + telemetry-arrival); see :mod:`plc_panel.scenario`.

Only absolute motion steps (MOVE / MOVE_PULSE) are persisted here: they are
the commands that must survive a crash without being lost or replayed into a
double-move (servo_core invariant #4 — absolute only).  Orchestration steps
(delay / scpi / pulse) are sequencer concerns and stay in the runner.

Mapping a :class:`plc_panel.models.ScanStep` onto servo_core's frozen, integer
:class:`MotionCommand` (no edit to ``core/`` required):

================  ===========================================================
MotionCommand     value
================  ===========================================================
target_position   position in micrometres   ``round(pos * 1000)``
speed             speed    in milli-units    ``round(spd * 1000)``
command_code      ``axis | PULSE_BIT(0x100)`` when the step is MOVE_PULSE
operator          free-text operator label
================  ===========================================================
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# servo_core lives one level up from python_panel (repo_root/core).  Make it
# importable without packaging so the core stays the single source of truth.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import (  # noqa: E402  (path bootstrap must precede this import)
    CommandRepository,
    CommandService,
    Database,
    DeadLetterReason,
    Event,
    EventBus,
    MotionCommand,
    QueuedCommand,
    QueueFullError,
)

from .models import ScanStep, StepType

#: Single FIFO channel: a scan scenario is one ordered sequence across axes,
#: so one channel preserves global step order and one monotonic ``seq``.
CHANNEL = "SCAN"

#: Fixed-point scale: millimetre REALs stored as integers in MotionCommand.
_SCALE = 1000.0
#: command_code bit marking a MOVE_PULSE (PLC emits its own measurement pulse).
_PULSE_BIT = 0x100

__all__ = [
    "CHANNEL",
    "Move",
    "MotionQueue",
    "decode_move",
    "DeadLetterReason",
    "QueueFullError",
]


@dataclass(frozen=True)
class Move:
    """An absolute move decoded back from a queued :class:`MotionCommand`."""

    axis: int
    pos: float
    spd: float
    pulse: bool


def _to_command(step: ScanStep, operator: str) -> MotionCommand:
    code = (step.axis & 0xFF) | (
        _PULSE_BIT if step.type is StepType.MOVE_PULSE else 0
    )
    return MotionCommand(
        target_position=round(step.pos * _SCALE),
        speed=round(step.spd * _SCALE),
        command_code=code,
        operator=operator,
    )


def decode_move(q: QueuedCommand) -> Move:
    """Decode a queued command back into an absolute :class:`Move`."""
    c = q.command
    return Move(
        axis=c.command_code & 0xFF,
        pos=c.target_position / _SCALE,
        spd=c.speed / _SCALE,
        pulse=bool(c.command_code & _PULSE_BIT),
    )


class MotionQueue:
    """Facade over servo_core's durable queue, scoped to the scan channel.

    Thread note: all queue access happens on the GUI thread (the runner), so a
    single SQLite connection is used.  The PLC worker thread never touches the
    database — it only does Modbus I/O.

    Events: producer/consumer transitions are published on :attr:`events`
    (servo_core's Qt-agnostic :class:`EventBus`).  ``CommandService.submit``
    already emits ``ENQUEUED``; the remaining transitions (SENT / DONE / RETRY
    / DEAD_LETTER) are published here, since on this PLC the executor is the
    runner rather than servo_core's :class:`MotionExecutor`.
    """

    def __init__(self, db_path: str, max_pending: int = 500) -> None:
        self._db = Database(db_path)
        self._repo = CommandRepository(self._db)
        self.events = EventBus()
        self._service = CommandService(
            self._repo, self.events, max_pending=max_pending
        )

    # -- producer ---------------------------------------------------------
    def submit_move(self, step: ScanStep, operator: str = "scan") -> QueuedCommand:
        """Persist one absolute move; raises ``QueueFullError`` on backpressure."""
        return self._service.submit(CHANNEL, _to_command(step, operator))

    # -- consumer (executor side) ----------------------------------------
    def claim_next(self) -> Optional[QueuedCommand]:
        """Atomically take the oldest PENDING move and mark it IN_FLIGHT."""
        return self._repo.claim_next(CHANNEL)

    def notify_sent(self, q: QueuedCommand) -> None:
        self.events.publish(Event.sent(q))

    def complete(self, q: QueuedCommand) -> None:
        """Close the command transactionally (queue + history in one tx)."""
        self._repo.complete(q)
        self.events.publish(Event.done(q))

    def mark_retry(self, q: QueuedCommand) -> int:
        attempts = self._repo.mark_retry(q)
        self.events.publish(Event.retry(q, attempts))
        return attempts

    def dead_letter(
        self, q: QueuedCommand, reason: DeadLetterReason, text: str = ""
    ) -> None:
        self._repo.dead_letter(q, reason, text)
        self.events.publish(Event.dead_letter(q, reason))

    # -- recovery / introspection ----------------------------------------
    def resume_in_flight(self) -> List[QueuedCommand]:
        """Moves claimed but not completed before a crash (for recovery)."""
        return self._repo.resume_in_flight(CHANNEL)

    def pending_count(self) -> int:
        return self._repo.pending_count(CHANNEL)

    def history(self, limit: int = 100) -> list:
        return self._repo.history(CHANNEL, limit)

    def dead_letters(self, limit: int = 100) -> list:
        return self._repo.dead_letters(CHANNEL, limit)
