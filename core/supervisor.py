"""Поток на канал + супервайзер.

ChannelWorker владеет драйвером (sync-клиент не шарится), исполнителем и
watchdog'ом. Падение одного канала не трогает другие. Recovery после старта
гейтится E-stop'ом: при небезопасном состоянии движение НЕ возобновляется
автоматически — ждём подтверждения оператора.
"""
from __future__ import annotations

import threading
import time

from .config import ChannelConfig
from .driver import ModbusDriver, ModbusError
from .events import Event, EventBus, EventType
from .executor import MotionExecutor
from .protocol import PlcProtocol
from .repository import CommandRepository
from .watchdog import ModbusWatchdog


class ChannelWorker:
    def __init__(
        self,
        cfg: ChannelConfig,
        driver: ModbusDriver,
        repo: CommandRepository,
        events: EventBus,
    ):
        self._cfg = cfg
        self._driver = driver
        self._repo = repo
        self._events = events
        self._proto = PlcProtocol(cfg.registers)
        self._executor = MotionExecutor(cfg, driver, self._proto, repo, events)
        self._watchdog = ModbusWatchdog(cfg, driver, events)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._recovery_blocked = False
        # Запрос оператора на разблокировку; исполняется в потоке канала,
        # т.к. только он владеет драйвером (инвариант №8).
        self._ack_requested = threading.Event()

    # --- жизненный цикл ---
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"worker-{self._cfg.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._watchdog.stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._driver.close()

    def acknowledge_safe_state(self) -> None:
        """Оператор подтвердил безопасное состояние после E-stop -> разблокировать.

        Не блокирует вызывающий (GUI) поток и не трогает драйвер из чужого
        потока: только выставляет запрос, recovery выполняет поток канала в
        ``_main_loop``. Подтверждение действует на текущий сеанс связи — при
        переподключении E-stop-гейт проверяется заново.
        """
        self._ack_requested.set()

    # --- основной цикл с авто-перезапуском канала ---
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._driver.connect()
                self._watchdog.start()
                self._safe_recovery()
                self._main_loop()
            except ModbusError as exc:
                self._events.publish(
                    Event(EventType.CONNECTION_LOST, self._cfg.name, detail=str(exc))
                )
            except Exception as exc:  # защита канала: поток не должен тихо умереть
                self._events.publish(
                    Event(
                        EventType.CONNECTION_LOST,
                        self._cfg.name,
                        detail=f"crash: {exc}",
                    )
                )
            finally:
                self._watchdog.stop()
                self._driver.close()
            if not self._stop.is_set():
                time.sleep(self._cfg.reconnect_backoff_s)

    def _safe_recovery(self) -> None:
        # Подтверждение из прошлого сеанса не должно снять свежую блокировку.
        self._ack_requested.clear()
        regs = self._driver.read_holding(
            self._cfg.registers.status_block_start,
            self._cfg.registers.status_block_length,
        )
        status = self._proto.decode_status(regs)
        if status.estop or status.guard_open:
            self._recovery_blocked = True
            self._events.publish(
                Event(
                    EventType.ESTOP,
                    self._cfg.name,
                    detail="recovery приостановлен: нужно подтверждение оператора",
                )
            )
            return
        self._executor.recover()

    def _main_loop(self) -> None:
        idle = max(self._cfg.poll_interval_s, 0.02)
        while not self._stop.is_set():
            if self._ack_requested.is_set():
                self._ack_requested.clear()
                self._recovery_blocked = False
                self._executor.recover()
                continue
            if self._recovery_blocked or not self._watchdog.connection_ok:
                time.sleep(idle)
                continue
            if not self._executor.process_one():
                time.sleep(idle)


class Supervisor:
    """Точка входа ядра: по ChannelWorker на канал поверх общей БД."""

    def __init__(self, repo: CommandRepository, events: EventBus):
        self._repo = repo
        self._events = events
        self._workers: dict[str, ChannelWorker] = {}

    def add_channel(self, cfg: ChannelConfig, driver: ModbusDriver) -> None:
        self._workers[cfg.name] = ChannelWorker(
            cfg, driver, self._repo, self._events
        )

    def start(self) -> None:
        for worker in self._workers.values():
            worker.start()

    def stop(self) -> None:
        for worker in self._workers.values():
            worker.stop()

    def acknowledge_safe_state(self, channel: str) -> None:
        self._workers[channel].acknowledge_safe_state()
