"""Full servo_core stack over the Easy521 *handshake* interface.

This is the "Вариант B" backbone: instead of executing moves on the GUI thread
(see :mod:`plc_panel.queue_backbone`), it runs servo_core's real
:class:`Supervisor` → :class:`ChannelWorker` → :class:`MotionExecutor` +
:class:`ModbusWatchdog` against a PLC that implements the handshake contract
(``ack_seq`` / ``command_state`` mirror, ready/estop/guard flags and a
bidirectional heartbeat).

It only works once the AutoShop ladder exposes that contract — see
``python_panel/HANDSHAKE_PROTOCOL.md`` for the register map and rung design.
Until then, use it headless with an injected mock driver (see
``plc_panel.supervised_demo``).

Command encoding — servo_core sends one generic command block
``[target_hi, target_lo, speed, command_code, seq_hi, seq_lo]``; the PLC
dispatcher reads ``command_code`` to pick the operation and axis:

    command_code = opcode | (axis << 4) | (PULSE_BIT when pulse)
        opcode 1 = MoveAbsolute   (target_position, speed used)
        opcode 2 = Stop           (speed = deceleration)
        opcode 3 = SetPosition    (target_position used)
        opcode 4 = Reset
    axis: 1=X, 2=Y, 3=Z      PULSE_BIT = 0x100

``target_position`` / ``speed`` are millimetre REALs scaled to integers
(``round(value * 1000)``) — i.e. micrometres / milli-units.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

# servo_core lives one level up from python_panel (repo_root/core).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import (  # noqa: E402  (path bootstrap must precede this import)
    ChannelConfig,
    CommandRepository,
    CommandService,
    Database,
    EventBus,
    ModbusDriver,
    MotionCommand,
    PymodbusTcpDriver,
    QueuedCommand,
    RegisterMap,
    Supervisor,
)

from .config import AppSettings

CHANNEL = "EASY521"

# --- command_code layout -------------------------------------------------
OP_MOVE_ABS = 1
OP_STOP = 2
OP_SET_POSITION = 3
OP_RESET = 4
_PULSE_BIT = 0x100
_SCALE = 1000.0

# --- handshake register map (free D-window; see HANDSHAKE_PROTOCOL.md) ----
#   command block  D400..D405   (Python -> PLC, one FC16)
#   status block   D410..D414   (PLC -> Python, one FC03)
#   watchdog       D420 (Py->PLC dead-man)   D421 (PLC->Py heartbeat)
HANDSHAKE_REGISTERS = RegisterMap(
    cmd_block_start=400, cmd_block_length=6,
    status_block_start=410, status_block_length=5,
    wd_python_to_plc=420, wd_plc_to_python=421,
)

#: Factory: build the transport driver for a channel (overridable for tests).
DriverFactory = Callable[[ChannelConfig], ModbusDriver]


def encode_command_code(opcode: int, axis: int, pulse: bool = False) -> int:
    return (opcode & 0x0F) | ((axis & 0x0F) << 4) | (_PULSE_BIT if pulse else 0)


def _move_command(axis: int, pos: float, spd: float, pulse: bool) -> MotionCommand:
    return MotionCommand(
        target_position=round(pos * _SCALE),
        speed=round(spd * _SCALE),
        command_code=encode_command_code(OP_MOVE_ABS, axis, pulse),
        operator="scan",
    )


class SupervisedBackbone:
    """Owns the durable queue *and* servo_core's per-channel worker stack.

    The :class:`Supervisor` runs a dedicated worker thread that pulls PENDING
    commands, performs the handshake with the PLC and confirms them by fact
    (``ack_seq == seq && state == DONE``); a :class:`ModbusWatchdog` thread
    keeps the bidirectional heartbeat alive.  The GUI only ``submit``s and
    listens to :attr:`events`.
    """

    def __init__(
        self,
        settings: AppSettings,
        db_path: Optional[str] = None,
        driver_factory: Optional[DriverFactory] = None,
    ) -> None:
        self._settings = settings
        db = db_path or str(Path(settings.queue_db_path).with_name(
            "supervised_queue.db"))

        self._cfg = ChannelConfig(
            name=CHANNEL,
            port=settings.plc_ip,                       # host for the TCP driver
            unit_id=1,
            registers=HANDSHAKE_REGISTERS,
            poll_interval_s=max(0.02, settings.poll_interval_ms / 1000.0),
            command_timeout_s=settings.move_timeout_ms / 1000.0,
            max_attempts=settings.max_move_attempts,
        )

        self._db = Database(db)
        self._repo = CommandRepository(self._db)
        self.events = EventBus()
        self._service = CommandService(
            self._repo, self.events, max_pending=self._cfg.max_pending)

        self._factory: DriverFactory = driver_factory or self._default_driver
        self._supervisor = Supervisor(self._repo, self.events)
        self._supervisor.add_channel(self._cfg, self._factory(self._cfg))

    def _default_driver(self, cfg: ChannelConfig) -> ModbusDriver:
        return PymodbusTcpDriver(
            host=cfg.port, port=self._settings.plc_port, unit_id=cfg.unit_id)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Start the channel worker + watchdog threads (and crash recovery)."""
        self._supervisor.start()

    def stop(self) -> None:
        self._supervisor.stop()

    def acknowledge_safe_state(self) -> None:
        """Operator confirms it is safe to resume after an E-stop -> recover."""
        self._supervisor.acknowledge_safe_state(CHANNEL)

    # ------------------------------------------------------------------ #
    # Producer / introspection                                          #
    # ------------------------------------------------------------------ #
    def submit_move(
        self, axis: int, pos: float, spd: float, pulse: bool = False
    ) -> QueuedCommand:
        """Enqueue one absolute move; the worker thread executes it."""
        return self._service.submit(CHANNEL, _move_command(axis, pos, spd, pulse))

    def pending_count(self) -> int:
        return self._repo.pending_count(CHANNEL)

    def history(self, limit: int = 100) -> list:
        return self._repo.history(CHANNEL, limit)

    def dead_letters(self, limit: int = 100) -> list:
        return self._repo.dead_letters(CHANNEL, limit)
