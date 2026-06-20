"""Headless proof of the durable scan-queue backbone — no Qt, no hardware.

Mirrors ``core/demo.py`` for python_panel's integration: it exercises the
servo_core queue exactly the way :class:`plc_panel.scenario.ScenarioRunner`
does (submit -> claim -> [crash] -> recover -> complete) and asserts the
effectively-once property across a simulated process restart.

Run::

    cd python_panel
    python -m plc_panel.queue_demo
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# Cyrillic output on a cp1252 Windows console would otherwise raise.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - older interpreters
    pass

from .models import ScanStep, StepType
from .queue_backbone import MotionQueue, decode_move


def _move(axis: int, pos: float, pulse: bool = False) -> ScanStep:
    return ScanStep(
        type=StepType.MOVE_PULSE if pulse else StepType.MOVE,
        axis=axis, pos=pos, spd=25.0,
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="scan_queue_demo_"))
    db_path = str(workdir / "scan_queue.db")
    print(f"== Демо надёжной очереди (БД: {db_path}) ==\n")

    try:
        # -- сессия 1: ставим план, две команды штатно, на третьей «крах» --
        q = MotionQueue(db_path)
        q.events.subscribe(lambda e: print(f"   event: {e.type.value} seq={e.seq} {e.detail}"))

        plan = [_move(1, 10.0), _move(1, 20.0), _move(1, 30.0, pulse=True)]
        print("Сессия 1: ставим 3 абсолютных перемещения в очередь")
        for step in plan:
            q.submit_move(step)
        assert q.pending_count() == 3, q.pending_count()

        print("\nСессия 1: штатно выполняем первые две (claim -> complete)")
        for _ in range(2):
            cmd = q.claim_next()
            assert cmd is not None
            move = decode_move(cmd)
            print(f"   -> ось {move.axis} в {move.pos} мм (seq={cmd.seq})")
            q.complete(cmd)
        assert q.pending_count() == 1

        print("\nСессия 1: берём третью, отправляем на ПЛК — и ПАДАЕМ до подтверждения")
        crashed = q.claim_next()
        assert crashed is not None
        q.notify_sent(crashed)
        crashed_seq = crashed.seq
        print(f"   команда seq={crashed_seq} осталась IN_FLIGHT (нет complete)")
        del q  # имитация гибели процесса: соединение/состояние теряются

        # -- сессия 2: рестарт на той же БД, recovery --------------------
        print("\nСессия 2: рестарт процесса на той же БД")
        q2 = MotionQueue(db_path)
        q2.events.subscribe(lambda e: print(f"   event: {e.type.value} seq={e.seq} {e.detail}"))

        inflight = q2.resume_in_flight()
        assert len(inflight) == 1, inflight
        assert inflight[0].seq == crashed_seq
        recovered = decode_move(inflight[0])
        print(f"   recovery нашёл незавершённую seq={crashed_seq} "
              f"(ось {recovered.axis} → {recovered.pos} мм)")

        # Идемпотентность: ПЛК уже отработал команду до краха (абсолютная
        # уставка), телеметрия показывает ось в точке -> закрываем БЕЗ
        # повторного движения. Никакого второго запуска.
        print("   ПЛК уже в целевой точке -> завершаем без повторного движения")
        q2.complete(inflight[0])

        assert q2.pending_count() == 0
        assert q2.resume_in_flight() == []

        # -- проверка истории: 3 уникальные команды, все DONE, без дублей --
        history = q2.history()
        seqs = sorted(row["seq"] for row in history)
        done = [row for row in history if row["status"] == "DONE"]
        print("\nИстория команд (seq -> статус):")
        for row in sorted(history, key=lambda r: r["seq"]):
            print(f"   seq={row['seq']}  {row['status']}")

        assert seqs == [1, 2, 3], seqs
        assert len(done) == 3, done
        assert q2.dead_letters() == []

        print(
            "\nOK: доставка at-least-once + исполнение at-most-once "
            "(идемпотентность по абсолютной уставке) = effectively-once.")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
