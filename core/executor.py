"""Оркестратор одного канала: handshake с ПЛК и транзакционное подтверждение.

Не знает про транспорт (через ModbusDriver) и про хранилище (через
CommandRepository) — только координирует. Подтверждение очереди привязано
к фактической отработке ПЛК (ack_seq == seq && state == DONE), а не к факту
успешной записи в шину.
"""
from __future__ import annotations

import time

from .config import ChannelConfig
from .driver import ModbusDriver, ModbusError
from .events import Event, EventBus
from .models import DeadLetterReason, PlcCommandState, QueuedCommand
from .protocol import PlcProtocol, PlcStatus
from .repository import CommandRepository


class MotionExecutor:
    def __init__(
        self,
        cfg: ChannelConfig,
        driver: ModbusDriver,
        protocol: PlcProtocol,
        repo: CommandRepository,
        events: EventBus,
    ):
        self._cfg = cfg
        self._driver = driver
        self._proto = protocol
        self._repo = repo
        self._events = events

    # ------------------------------------------------------------------ #
    # Публичные шаги
    # ------------------------------------------------------------------ #
    def process_one(self) -> bool:
        """Обрабатывает одну команду из очереди. False, если очередь пуста."""
        q = self._repo.claim_next(self._cfg.name)
        if q is None:
            return False
        self._run(q, recovery=False)
        return True

    def recover(self) -> None:
        """Вызывается один раз при старте: сверяет in-flight команды с ПЛК."""
        for q in self._repo.resume_in_flight(self._cfg.name):
            self._events.publish(Event.recovery(q))
            self._run(q, recovery=True)

    # ------------------------------------------------------------------ #
    # Ядро handshake
    # ------------------------------------------------------------------ #
    def _run(self, q: QueuedCommand, recovery: bool) -> None:
        try:
            status = self._read_status()

            # 1) safety-gate: при E-stop / открытой защите не двигаемся.
            if status.estop or status.guard_open:
                self._to_dead_letter(
                    q, DeadLetterReason.SAFETY_LOCK, "E-stop/guard активны"
                )
                return

            # 2) идемпотентность/recovery: ПЛК уже видел этот seq?
            if status.ack_seq == q.seq:
                if status.state == PlcCommandState.DONE:
                    self._finish_done(q)
                    return
                if status.state in (PlcCommandState.ERROR, PlcCommandState.ABORTED):
                    self._finish_plc_error(q, status)
                    return
                if status.state in (PlcCommandState.RUNNING, PlcCommandState.RECEIVED):
                    self._wait_completion(q)
                    return
            # Иначе ПЛК этот seq не обрабатывал — можно (пере)отправить.

            # 3) штатная отправка.
            self._wait_ready()
            self._send(q)
            self._wait_completion(q)

        except ModbusError as exc:
            # Транспортная ошибка != ошибка команды: на повтор.
            self._handle_retryable(q, str(exc))

    def _read_status(self) -> PlcStatus:
        r = self._cfg.registers
        regs = self._driver.read_holding(
            r.status_block_start, r.status_block_length
        )
        return self._proto.decode_status(regs)

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self._cfg.command_timeout_s
        while True:
            status = self._read_status()
            if status.estop or status.guard_open:
                raise ModbusError("Небезопасное состояние во время ожидания READY")
            if status.ready:
                return
            if time.monotonic() > deadline:
                raise ModbusError("ПЛК не перешёл в READY за таймаут")
            time.sleep(self._cfg.poll_interval_s)

    def _send(self, q: QueuedCommand) -> None:
        r = self._cfg.registers
        block = self._proto.encode_command(q.command, q.seq)
        # Один FC16: seq логически последний -> ПЛК не увидит «рваную» команду.
        self._driver.write_registers(r.cmd_block_start, block)
        self._events.publish(Event.sent(q))

    def _wait_completion(self, q: QueuedCommand) -> None:
        deadline = time.monotonic() + self._cfg.command_timeout_s
        while True:
            status = self._read_status()
            if status.ack_seq == q.seq:
                if status.state == PlcCommandState.DONE:
                    self._finish_done(q)
                    return
                if status.state in (PlcCommandState.ERROR, PlcCommandState.ABORTED):
                    self._finish_plc_error(q, status)
                    return
            if time.monotonic() > deadline:
                self._handle_timeout(q)
                return
            time.sleep(self._cfg.poll_interval_s)

    # ------------------------------------------------------------------ #
    # Завершения
    # ------------------------------------------------------------------ #
    def _finish_done(self, q: QueuedCommand) -> None:
        self._repo.complete(q)
        self._events.publish(Event.done(q))

    def _finish_plc_error(self, q: QueuedCommand, status: PlcStatus) -> None:
        self._to_dead_letter(
            q, DeadLetterReason.PLC_ERROR, f"err_code={status.error_code}"
        )

    def _handle_timeout(self, q: QueuedCommand) -> None:
        self._retry_or_dead_letter(
            q, DeadLetterReason.TIMEOUT_LIMIT, "превышен таймаут выполнения"
        )

    def _handle_retryable(self, q: QueuedCommand, msg: str) -> None:
        self._retry_or_dead_letter(q, DeadLetterReason.TIMEOUT_LIMIT, msg)

    def _retry_or_dead_letter(
        self, q: QueuedCommand, reason: DeadLetterReason, msg: str
    ) -> None:
        if q.attempts + 1 >= self._cfg.max_attempts:
            self._to_dead_letter(q, reason, msg)
        else:
            attempts = self._repo.mark_retry(q)
            self._events.publish(Event.retry(q, attempts))

    def _to_dead_letter(
        self, q: QueuedCommand, reason: DeadLetterReason, msg: str
    ) -> None:
        self._repo.dead_letter(q, reason, msg)
        self._events.publish(Event.dead_letter(q, reason))
