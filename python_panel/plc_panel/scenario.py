"""Queue-backed scan sequencer.

The runner walks an ordered list of :class:`ScanStep`.  Absolute moves
(MOVE / MOVE_PULSE) are routed through the durable
:class:`plc_panel.queue_backbone.MotionQueue` (servo_core's transactional
queue); orchestration steps (delay / scpi / pulse) run inline.

Per move the runner: enqueues it, claims it (PENDING -> IN_FLIGHT, atomically),
issues it on the real PLC via :class:`PlcService`, and marks it *done* only
once telemetry shows the axis has actually reached the target.  That is the
"ack by fact" rule from servo_core's CLAUDE.md (invariant #2), realised with
position feedback in place of an ``ack_seq`` handshake register.

Failure handling mirrors the core: a move that does not arrive within
``move_timeout_ms`` is retried up to ``max_move_attempts`` and then sent to the
dead-letter queue (which aborts the scenario).

Crash recovery (:meth:`recover`, call it on every (re)connect): any move left
IN_FLIGHT by a previous crashed run is reconciled against live telemetry.  If
the axis is already at the target the command is completed without moving again
(idempotent); otherwise the *absolute* move is re-issued (safe to repeat —
invariant #4).  Recovery is gated by E-stop (invariant #5): while E-stop is
asserted, motion is not resumed until :meth:`acknowledge_safe_state` is called.
"""

from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from .config import AppSettings
from .models import ScanStep, StepType
from .queue_backbone import (
    DeadLetterReason,
    MotionQueue,
    Move,
    QueueFullError,
    decode_move,
)
from .service import PlcService

#: Settle time before advancing past a fired hardware pulse (ms).
_PULSE_SETTLE_MS = 350
_SCPI_SETTLE_MS = 150
#: Dwell after a move_pulse arrival, giving the PLC time to emit its own
#: measurement pulse (M14 + Done -> TPR -> Y0) before the next step moves.
_MOVE_PULSE_DWELL_MS = 200
#: Delay between auto Servo-ON and the first step, so the drives finish
#: enabling before the first motion command is issued.
_SERVO_ENABLE_DELAY_MS = 500


class ScenarioRunner(QObject):
    """Runs a scan sequence step by step over a durable :class:`MotionQueue`."""

    step_changed = pyqtSignal(int)   # active index, or -1 when idle
    finished = pyqtSignal()
    log = pyqtSignal(str)

    def __init__(
        self,
        service: PlcService,
        settings: AppSettings,
        queue: MotionQueue,
        session=None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._settings = settings
        self._queue = queue
        self._session = session   # ScanSession | None (crash-resume persistence)

        self._steps: List[ScanStep] = []
        self._index = -1
        self._active = False
        self._paused = False
        self._parked = False   # paused exactly at a step boundary, awaiting resume

        # The single move currently being executed (claimed, in flight).
        self._inflight = None            # QueuedCommand | None
        self._inflight_move: Optional[Move] = None

        self._last_telemetry = None
        # Recovery: commands found IN_FLIGHT from a previous run, plus the tail
        # still to reconcile after the one being re-issued completes.
        self._recovery_pending: list = []
        self._recovery_rest: list = []
        self._recovery_blocked_logged = False
        self._safe_ack = False

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_move_timeout)

        self._service.telemetry.connect(self._on_telemetry)
        self._queue.events.subscribe(self._on_core_event)

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def current_index(self) -> int:
        return self._index

    # ------------------------------------------------------------------ #
    # Control                                                            #
    # ------------------------------------------------------------------ #
    def start(self, steps: List[ScanStep], start_index: int = 0) -> None:
        if not steps:
            self.log.emit("Ошибка: Сценарий пуст!")
            return
        if self._active:
            self.log.emit("Сценарий уже выполняется.")
            return

        active = list(self._settings.active_axes)
        self.log.emit(
            f"Авто-подготовка: включение подключённых приводов {active} (Servo ON)...")
        for axis in active:
            self._service.set_power(axis, True)

        self._steps = list(steps)
        self._index = start_index if 0 <= start_index < len(steps) else 0
        self._active = True
        self._paused = False
        self._parked = False
        if self._session is not None:
            self._session.begin(self._steps, self._index)
        self.log.emit(
            f"Запуск сценария со строки №{self._index + 1} "
            f"(старт через {_SERVO_ENABLE_DELAY_MS} мс после Servo ON)")
        # Defer the first step so the drives have time to enable.
        QTimer.singleShot(_SERVO_ENABLE_DELAY_MS, self._execute)

    def pause(self) -> None:
        if not self._active or self._paused:
            return
        self._paused = True
        self.log.emit("Сканирование на паузе (после завершения текущего шага).")

    def resume(self) -> None:
        if not self._active or not self._paused:
            return
        self._paused = False
        self.log.emit("Сканирование продолжено.")
        # Only kick the machine if it actually parked at a boundary; if a timer
        # / in-flight move is still pending it will continue on its own.
        if self._parked:
            self._parked = False
            self._execute()

    @property
    def is_paused(self) -> bool:
        return self._paused

    def stop(self) -> None:
        if not self._active and self._inflight is None:
            return
        self._active = False
        self._paused = False
        self._parked = False
        self._index = -1
        self._timeout.stop()
        # A claimed-but-unfinished move is intentionally left IN_FLIGHT in the
        # database: recover() reconciles it later. We never silently complete a
        # move the PLC may not have finished.
        self._inflight = None
        self._inflight_move = None
        if self._session is not None:
            self._session.end()
        self.step_changed.emit(-1)
        self.log.emit("Автоматическое выполнение сценария прервано оператором.")

    def acknowledge_safe_state(self) -> None:
        """Operator confirms it is safe to resume after an E-stop -> unblock."""
        self._safe_ack = True
        self.log.emit("Оператор подтвердил безопасное состояние — recovery разблокирован.")
        self._try_recovery()

    # ------------------------------------------------------------------ #
    # Crash recovery                                                     #
    # ------------------------------------------------------------------ #
    def recover(self) -> None:
        """Reconcile moves left IN_FLIGHT by a previous crashed run."""
        if self._active:
            return
        pending = self._queue.resume_in_flight()
        if not pending:
            return
        self._recovery_pending = list(pending)
        self._recovery_blocked_logged = False
        self.log.emit(
            f"Recovery: найдено незавершённых команд из прошлого запуска: "
            f"{len(pending)}.")
        # Acts as soon as a telemetry frame is available and the cell is safe.
        self._try_recovery()

    def _try_recovery(self) -> None:
        if not self._recovery_pending:
            return
        if self._last_telemetry is None:
            return  # wait for the first telemetry frame to assess state
        if not self._safe_to_move() and not self._safe_ack:
            if not self._recovery_blocked_logged:
                self.log.emit(
                    "Recovery приостановлен: активен E-stop. "
                    "Требуется подтверждение оператора (acknowledge_safe_state).")
                self._recovery_blocked_logged = True
            return
        pending = self._recovery_pending
        self._recovery_pending = []
        self._reconcile(pending)

    def _reconcile(self, pending: list) -> None:
        """Complete moves already at target; re-issue the first that is not."""
        remaining = []
        for q in pending:
            move = decode_move(q)
            if self._at_target(move):
                self._queue.complete(q)
                self.log.emit(
                    f"Recovery: seq={q.seq} уже в целевой точке — "
                    f"без повторного движения.")
            else:
                remaining.append(q)
        if not remaining:
            self.log.emit("Recovery завершён.")
            return
        q = remaining[0]
        move = decode_move(q)
        self._recovery_rest = remaining[1:]
        self.log.emit(
            f"Recovery: переотправка абсолютной команды seq={q.seq} "
            f"(ось {move.axis} → {move.pos} мм).")
        self._drive(q, move)

    # ------------------------------------------------------------------ #
    # Step machine                                                       #
    # ------------------------------------------------------------------ #
    def _execute(self) -> None:
        if not self._active:
            return
        if self._index >= len(self._steps):
            self._finish_ok()
            return

        self.step_changed.emit(self._index)
        if self._session is not None:
            self._session.update(self._index)
        step = self._steps[self._index]

        if step.is_move:
            if step.axis not in self._settings.active_axes:
                # No servo on this axis -> arrival would never be reported.
                # Skip the step instead of hanging on a never-completing move.
                self.log.emit(
                    f"Шаг №{self._index + 1}: ось {step.axis} не подключена "
                    f"— движение пропущено.")
                QTimer.singleShot(0, self._advance)
                return
            self._dispatch_move(step)
        elif step.type is StepType.DELAY:
            QTimer.singleShot(step.ms, self._advance)
        elif step.type is StepType.SCPI:
            self._service.send_scpi(step.cmd, self._settings.scpi_ip,
                                    self._settings.scpi_port)
            QTimer.singleShot(_SCPI_SETTLE_MS, self._advance)
        elif step.type is StepType.PULSE:
            self._service.pulse_trigger()
            QTimer.singleShot(_PULSE_SETTLE_MS, self._advance)

    def _dispatch_move(self, step: ScanStep) -> None:
        try:
            self._queue.submit_move(step)
        except QueueFullError as exc:
            self._abort(f"очередь переполнена ({exc})")
            return
        q = self._queue.claim_next()
        if q is None:
            # Nothing to run (already drained) — just move on.
            self._advance()
            return
        self._drive(q, decode_move(q))

    def _drive(self, q, move: Move) -> None:
        """Issue one claimed move on the PLC and arm its completion timeout."""
        self._inflight = q
        self._inflight_move = move
        self._service.move_absolute(move.axis, move.pos, move.spd, move.pulse)
        self._queue.notify_sent(q)
        self._timeout.start(self._settings.move_timeout_ms)

    def _advance(self) -> None:
        if not self._active:
            return
        self._index += 1
        if self._paused:
            # Park at this index; resume() will run it.
            self._parked = True
            return
        self._execute()

    def _finish_ok(self) -> None:
        self._active = False
        self._paused = False
        self._parked = False
        self._index = -1
        if self._session is not None:
            self._session.end()
        self.step_changed.emit(-1)
        self.log.emit("Сценарий сканирования выполнен полностью!")
        self.finished.emit()

    def _abort(self, reason: str) -> None:
        self._active = False
        self._paused = False
        self._parked = False
        self._index = -1
        self._timeout.stop()
        if self._session is not None:
            self._session.end()
        self.step_changed.emit(-1)
        self.log.emit(f"Сценарий остановлен: {reason}.")
        self.finished.emit()

    # ------------------------------------------------------------------ #
    # PLC feedback                                                       #
    # ------------------------------------------------------------------ #
    def _on_telemetry(self, telemetry) -> None:
        self._last_telemetry = telemetry
        self._try_recovery()

        if self._inflight is None:
            return
        move = self._inflight_move
        axis = telemetry.axes.get(move.axis)
        if axis is None:
            return
        if abs(axis.position - move.pos) >= self._settings.arrival_tolerance_mm:
            return

        # Arrived: close the command transactionally (ack by fact).
        self._timeout.stop()
        q = self._inflight
        self._inflight = None
        self._inflight_move = None
        self._queue.complete(q)

        if self._active:
            if move.pulse:
                self.log.emit(
                    "[Аппаратный замер]: точка достигнута, "
                    "ПЛК отрабатывает импульс (M14).")
                QTimer.singleShot(_MOVE_PULSE_DWELL_MS, self._advance)
            else:
                self._advance()
        else:
            # This was a recovery move, not an active scenario step.
            self.log.emit(f"Recovery: seq={q.seq} завершена (ось в точке).")
            if self._recovery_rest:
                rest = self._recovery_rest
                self._recovery_rest = []
                self._reconcile(rest)
            else:
                self.log.emit("Recovery завершён.")

    def _on_move_timeout(self) -> None:
        if self._inflight is None:
            return
        q = self._inflight
        self._inflight = None
        self._inflight_move = None

        if q.attempts + 1 >= self._settings.max_move_attempts:
            self._queue.dead_letter(
                q, DeadLetterReason.TIMEOUT_LIMIT, "превышен таймаут движения")
            self.log.emit(
                f"Команда seq={q.seq} не выполнена за таймаут — dead-letter.")
            self._abort("команда не подтверждена ПЛК (dead-letter)")
        else:
            self._queue.mark_retry(q)
            self.log.emit(f"Команда seq={q.seq}: таймаут, повтор.")
            again = self._queue.claim_next()
            if again is not None:
                self._drive(again, decode_move(again))

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _at_target(self, move: Move) -> bool:
        if self._last_telemetry is None:
            return False
        axis = self._last_telemetry.axes.get(move.axis)
        if axis is None:
            return False
        return abs(axis.position - move.pos) < self._settings.arrival_tolerance_mm

    def _safe_to_move(self) -> bool:
        """False while the hardware E-stop input (X3) is asserted."""
        t = self._last_telemetry
        if t is None:
            return False
        return not (len(t.inputs) > 3 and t.inputs[3])

    def _on_core_event(self, event) -> None:
        """Mirror servo_core queue events into the operator journal."""
        text = {
            "ENQUEUED": f"Очередь: команда seq={event.seq} поставлена.",
            "SENT": f"Очередь: seq={event.seq} отправлена на ПЛК.",
            "DONE": f"Очередь: seq={event.seq} подтверждена (ось в точке).",
            "RETRY": f"Очередь: seq={event.seq} повтор ({event.detail}).",
            "DEAD_LETTER": f"Очередь: seq={event.seq} в dead-letter ({event.detail}).",
        }.get(event.type.value)
        if text:
            self.log.emit(text)
