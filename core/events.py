"""Шина событий, не зависящая от Qt.

Ядро публикует Event. Подписчиком может быть Qt-мост, логгер или тест.
Это разрывает зависимость ядра от PyQt: ядро тестируется headless.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class EventType(str, Enum):
    ENQUEUED = "ENQUEUED"
    SENT = "SENT"
    DONE = "DONE"
    RETRY = "RETRY"
    DEAD_LETTER = "DEAD_LETTER"
    RECOVERY = "RECOVERY"
    CONNECTION_LOST = "CONNECTION_LOST"
    CONNECTION_RESTORED = "CONNECTION_RESTORED"
    ESTOP = "ESTOP"


@dataclass(frozen=True)
class Event:
    type: EventType
    channel: str = ""
    seq: int = 0
    detail: str = ""
    payload: Any = None

    # Фабрики для краткости в вызовах ядра.
    @staticmethod
    def enqueued(q) -> "Event":
        return Event(EventType.ENQUEUED, q.channel, q.seq)

    @staticmethod
    def sent(q) -> "Event":
        return Event(EventType.SENT, q.channel, q.seq)

    @staticmethod
    def done(q) -> "Event":
        return Event(EventType.DONE, q.channel, q.seq)

    @staticmethod
    def retry(q, attempts: int) -> "Event":
        return Event(EventType.RETRY, q.channel, q.seq, f"attempt {attempts}")

    @staticmethod
    def dead_letter(q, reason) -> "Event":
        return Event(EventType.DEAD_LETTER, q.channel, q.seq, reason.value)

    @staticmethod
    def recovery(q) -> "Event":
        return Event(EventType.RECOVERY, q.channel, q.seq)


class EventBus:
    """Потокобезопасный publish/subscribe."""

    def __init__(self):
        self._subscribers: list[Callable[[Event], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def publish(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                # Сбой подписчика не должен ронять ядро.
                pass
