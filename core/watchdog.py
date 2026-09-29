"""Двунаправленный watchdog в отдельном потоке.

  • Python -> ПЛК: инкрементирует регистр (dead-man). Если ПЛК перестаёт
    видеть инкремент — он сам переводит привод в безопасное состояние.
  • ПЛК -> Python: читает heartbeat-счётчик ПЛК. Если тот застыл дольше
    таймаута — фиксируем потерю связи/зависание.

Намеренно НЕ смешан с обработкой команд.
"""
from __future__ import annotations

import threading
import time

from .config import ChannelConfig
from .driver import ModbusDriver, ModbusError
from .events import Event, EventBus, EventType


class ModbusWatchdog:
    def __init__(self, cfg: ChannelConfig, driver: ModbusDriver, events: EventBus):
        self._cfg = cfg
        self._driver = driver
        self._events = events
        self._counter = 0
        self._last_plc_hb: int | None = None
        self._last_change = time.monotonic()
        self._connected = True
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @property
    def connection_ok(self) -> bool:
        with self._lock:
            return self._connected

    def start(self) -> None:
        self._stop.clear()
        self._last_change = time.monotonic()
        self._thread = threading.Thread(
            target=self._loop, name=f"wd-{self._cfg.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        r = self._cfg.registers
        while not self._stop.is_set():
            try:
                self._counter = (self._counter + 1) & 0xFFFF
                self._driver.write_register(r.wd_python_to_plc, self._counter)
                hb = self._driver.read_holding(r.wd_plc_to_python, 1)[0]
                self._evaluate(hb)
            except ModbusError as exc:
                self._set_connection(False, str(exc))
            self._stop.wait(self._cfg.watchdog_interval_s)

    def _evaluate(self, hb: int) -> None:
        now = time.monotonic()
        if hb != self._last_plc_hb:
            self._last_plc_hb = hb
            self._last_change = now
            self._set_connection(True, "")
        elif now - self._last_change > self._cfg.watchdog_timeout_s:
            self._set_connection(False, "heartbeat ПЛК застыл")

    def _set_connection(self, ok: bool, detail: str) -> None:
        with self._lock:
            changed = ok != self._connected
            self._connected = ok
        if changed:
            etype = (
                EventType.CONNECTION_RESTORED if ok else EventType.CONNECTION_LOST
            )
            self._events.publish(Event(etype, self._cfg.name, detail=detail))
