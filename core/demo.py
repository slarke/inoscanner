"""Headless-демонстрация ядра без железа и без Qt.

Сценарии:
  1. Обычное прохождение команд через очередь -> ПЛК -> DONE.
  2. Симуляция краша между отправкой и подтверждением: команда остаётся
     IN_FLIGHT в БД; после «перезапуска» recovery сверяет seq с ПЛК и НЕ
     запускает движение второй раз (идемпотентность).
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from servo_core.config import ChannelConfig, RegisterMap
from servo_core.database import Database
from servo_core.events import EventBus, Event
from servo_core.mock import MockModbusDriver, MockPlc
from servo_core.models import MotionCommand
from servo_core.protocol import PlcProtocol
from servo_core.repository import CommandRepository
from servo_core.service import CommandService
from servo_core.supervisor import Supervisor


def log_event(e: Event) -> None:
    print(f"  [event] {e.type.value:<20} {e.channel} seq={e.seq} {e.detail}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "servo.db"
    registers = RegisterMap()
    cfg = ChannelConfig(name="COM3", port="mock", registers=registers,
                        poll_interval_s=0.01, command_timeout_s=5.0)

    # Общий ПЛК-мок переживает «перезапуск» приложения (как реальный ПЛК).
    plc = MockPlc(registers, move_time=0.15)

    print("=== Сценарий 1: штатное прохождение ===")
    db = Database(db_path)
    repo = CommandRepository(db)
    events = EventBus()
    events.subscribe(log_event)
    service = CommandService(repo, events, max_pending=cfg.max_pending)

    supervisor = Supervisor(repo, events)
    supervisor.add_channel(cfg, MockModbusDriver(plc))
    supervisor.start()

    for i in range(3):
        q = service.submit("COM3", MotionCommand(
            target_position=1000 * (i + 1), speed=500, command_code=1,
            operator="demo"))
        print(f"  submit seq={q.seq} target={q.command.target_position}")

    time.sleep(2.0)
    supervisor.stop()

    print("\n  История команд:")
    for row in reversed(repo.history("COM3")):
        print(f"    seq={row['seq']} status={row['status']}")
    last_seq_after_normal = plc._last_processed_seq  # noqa: SLF001 (демонстрация)
    print(f"  ПЛК last_processed_seq = {last_seq_after_normal}")

    print("\n=== Сценарий 2: краш между отправкой и подтверждением ===")
    # Имитируем «зависшую» IN_FLIGHT-команду в БД, как если бы Python упал
    # сразу после write_registers, но до complete(). ПЛК её уже отработал.
    crash_cmd = MotionCommand(target_position=9999, speed=500, command_code=1)
    q = repo.enqueue("COM3", crash_cmd)
    claimed = repo.claim_next("COM3")          # state -> IN_FLIGHT
    proto = PlcProtocol(registers)
    drv = MockModbusDriver(plc)
    drv.connect()
    drv.write_registers(registers.cmd_block_start,
                        proto.encode_command(claimed.command, claimed.seq))
    print(f"  отправили seq={claimed.seq}, ждём отработки ПЛК, затем 'краш'")
    time.sleep(0.3)                            # ПЛК успевает выполнить DONE
    print(f"  ПЛК last_processed_seq = {plc._last_processed_seq} (команда выполнена)")
    print(f"  в БД команда осталась IN_FLIGHT: "
          f"{len(repo.resume_in_flight('COM3'))} шт.")

    print("\n  --- перезапуск приложения, recovery ---")
    seq_before = plc._last_processed_seq
    supervisor2 = Supervisor(repo, events)
    supervisor2.add_channel(cfg, MockModbusDriver(plc))
    supervisor2.start()
    time.sleep(1.0)
    supervisor2.stop()
    seq_after = plc._last_processed_seq

    print(f"\n  ПЛК last_processed_seq до recovery = {seq_before}, после = {seq_after}")
    assert seq_after == seq_before, "ДВОЙНОЙ ЗАПУСК! идемпотентность нарушена"
    assert len(repo.resume_in_flight("COM3")) == 0, "команда не закрыта recovery"
    print("  OK: команда закрыта recovery БЕЗ повторного движения "
          "(effectively-once подтверждён).")


if __name__ == "__main__":
    main()
