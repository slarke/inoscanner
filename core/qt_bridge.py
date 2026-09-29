"""PyQt-мост. Единственная точка связи ядра с Qt.

Ядро от Qt не зависит. Этот модуль подписывается на EventBus и превращает
Event в Qt-сигналы. publish() вызывается из worker-потоков, поэтому сигналы
доставляются в GUI-поток через очередь Qt (queued connection) — без гонок
с виджетами, при условии что получатель (слот) живёт в GUI-потоке.

Импорт PyQt6 защищён: ядро можно использовать headless (тесты, сервис).
Если PyQt6 не установлен, QtEventBridge остаётся определён, но падает при
ПОПЫТКЕ СОЗДАНИЯ, а не при импорте модуля.
"""
from __future__ import annotations

from .events import Event, EventBus, EventType

try:
    from PyQt6.QtCore import QObject, pyqtSignal
    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _QT_AVAILABLE = False


if _QT_AVAILABLE:

    class QtEventBridge(QObject):
        """Превращает Event ядра в Qt-сигналы для подключения к слотам GUI."""

        command_enqueued = pyqtSignal(str, int)        # channel, seq
        command_sent = pyqtSignal(str, int)
        command_done = pyqtSignal(str, int)
        command_retry = pyqtSignal(str, int, str)      # channel, seq, detail
        command_dead_letter = pyqtSignal(str, int, str)
        recovery_started = pyqtSignal(str, int)
        connection_lost = pyqtSignal(str, str)         # channel, detail
        connection_restored = pyqtSignal(str, str)
        estop_detected = pyqtSignal(str, str)

        def __init__(self, event_bus: EventBus, parent=None):
            super().__init__(parent)
            event_bus.subscribe(self._on_event)

        def _on_event(self, e: Event) -> None:
            dispatch = {
                EventType.ENQUEUED: lambda: self.command_enqueued.emit(e.channel, e.seq),
                EventType.SENT: lambda: self.command_sent.emit(e.channel, e.seq),
                EventType.DONE: lambda: self.command_done.emit(e.channel, e.seq),
                EventType.RETRY: lambda: self.command_retry.emit(e.channel, e.seq, e.detail),
                EventType.DEAD_LETTER: lambda: self.command_dead_letter.emit(
                    e.channel, e.seq, e.detail
                ),
                EventType.RECOVERY: lambda: self.recovery_started.emit(e.channel, e.seq),
                EventType.CONNECTION_LOST: lambda: self.connection_lost.emit(
                    e.channel, e.detail
                ),
                EventType.CONNECTION_RESTORED: lambda: self.connection_restored.emit(
                    e.channel, e.detail
                ),
                EventType.ESTOP: lambda: self.estop_detected.emit(e.channel, e.detail),
            }
            handler = dispatch.get(e.type)
            if handler is not None:
                handler()

else:

    class QtEventBridge:  # type: ignore[no-redef]
        """Заглушка: PyQt6 не установлен. Конструктор сообщает об этом явно."""

        def __init__(self, *_args, **_kwargs):
            raise RuntimeError(
                "PyQt6 не установлен — QtEventBridge недоступен. "
                "Используйте EventBus.subscribe() напрямую для headless-режима."
            )
