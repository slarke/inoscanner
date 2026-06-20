"""Headless proof of the supervised (handshake) backbone — no Qt, no hardware.

Drives :class:`plc_panel.supervised_backbone.SupervisedBackbone` against
servo_core's :class:`MockPlc` — the reference implementation of the very PLC
handshake the AutoShop ladder must provide (``ack_seq`` / ``command_state`` /
heartbeat / ``last_processed_seq`` dedup).  It runs the *real*
``Supervisor`` → ``MotionExecutor`` + ``ModbusWatchdog`` threads and asserts
effectively-once across a simulated crash + restart.

Run::

    cd python_panel
    python -m plc_panel.supervised_demo
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

# Importing the backbone bootstraps repo_root onto sys.path for `core`.
from .config import AppSettings
from .supervised_backbone import (
    CHANNEL,
    HANDSHAKE_REGISTERS,
    OP_MOVE_ABS,
    SupervisedBackbone,
    encode_command_code,
)

from core import Event, MotionCommand  # noqa: E402
from core.mock import MockModbusDriver, MockPlc  # noqa: E402
from core.protocol import PlcProtocol  # noqa: E402


def _log(e: Event) -> None:
    print(f"   [event] {e.type.value:<18} {e.channel} seq={e.seq} {e.detail}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="supervised_demo_"))
    db_path = str(workdir / "supervised_queue.db")
    settings = AppSettings(queue_db_path=db_path, move_timeout_ms=5000,
                           poll_interval_ms=20)

    # One mock PLC survives the "process restart" (like real hardware).
    plc = MockPlc(HANDSHAKE_REGISTERS, move_time=0.15)
    factory = lambda cfg: MockModbusDriver(plc)  # noqa: E731 (shared mock PLC)

    print(f"== Демо handshake-стека (БД: {db_path}) ==\n")
    try:
        # -- Сценарий 1: штатное прохождение через Supervisor/Executor -----
        print("Сценарий 1: 3 команды через очередь -> handshake -> DONE")
        b1 = SupervisedBackbone(settings, db_path=db_path, driver_factory=factory)
        b1.events.subscribe(_log)
        b1.start()
        for i in range(3):
            q = b1.submit_move(axis=1, pos=10.0 * (i + 1), spd=25.0)
            print(f"   submit seq={q.seq} target={q.command.target_position}")
        time.sleep(2.0)
        b1.stop()

        hist = b1.history()
        print("\n   История:", sorted((r["seq"], r["status"]) for r in hist))
        assert all(r["status"] == "DONE" for r in hist), hist
        assert b1.pending_count() == 0
        seq_after_normal = plc._last_processed_seq  # noqa: SLF001 (демонстрация)
        print(f"   ПЛК last_processed_seq = {seq_after_normal}")

        # -- Сценарий 2: краш между отправкой и подтверждением -------------
        print("\nСценарий 2: краш после write_registers, до complete()")
        b1._repo.enqueue(CHANNEL, MotionCommand(  # noqa: SLF001 (инъекция краша)
            target_position=99000, speed=25000,
            command_code=encode_command_code(OP_MOVE_ABS, axis=1)))
        claimed = b1._repo.claim_next(CHANNEL)  # noqa: SLF001 -> IN_FLIGHT
        proto = PlcProtocol(HANDSHAKE_REGISTERS)
        drv = MockModbusDriver(plc)
        drv.connect()
        drv.write_registers(HANDSHAKE_REGISTERS.cmd_block_start,
                            proto.encode_command(claimed.command, claimed.seq))
        print(f"   отправили seq={claimed.seq}; ждём отработки ПЛК, затем 'краш'")
        time.sleep(0.3)
        seq_before = plc._last_processed_seq  # noqa: SLF001
        print(f"   ПЛК выполнил команду (last_processed_seq={seq_before}); "
              f"в БД осталось IN_FLIGHT: {len(b1._repo.resume_in_flight(CHANNEL))}")  # noqa: SLF001

        # -- перезапуск приложения -> recovery -----------------------------
        print("\n   --- перезапуск, recovery через Supervisor.start() ---")
        b2 = SupervisedBackbone(settings, db_path=db_path, driver_factory=factory)
        b2.events.subscribe(_log)
        b2.start()
        time.sleep(1.0)
        b2.stop()
        seq_recovered = plc._last_processed_seq  # noqa: SLF001

        assert seq_recovered == seq_before, "ДВОЙНОЙ ЗАПУСК! идемпотентность нарушена"
        assert b2.pending_count() == 0
        assert len(b2._repo.resume_in_flight(CHANNEL)) == 0  # noqa: SLF001
        print(f"\n   last_processed_seq до recovery={seq_before}, после={seq_recovered}")
        print("\nOK: recovery закрыл зависшую команду БЕЗ повторного движения "
              "(handshake + idempotency = effectively-once).")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
