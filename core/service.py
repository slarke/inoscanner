"""Публичный фасад для GUI — единственная точка постановки команд.

Потокобезопасен сам по себе (всё через транзакции БД). GUI вызывает submit()
и подписывается на события через EventBus / Qt-мост.
"""
from __future__ import annotations

from .events import Event, EventBus
from .models import MotionCommand, QueuedCommand
from .repository import CommandRepository


class QueueFullError(Exception):
    """Очередь канала переполнена (backpressure)."""


class CommandService:
    def __init__(
        self,
        repo: CommandRepository,
        events: EventBus,
        max_pending: int = 100,
    ):
        self._repo = repo
        self._events = events
        self._max_pending = max_pending

    def submit(self, channel: str, command: MotionCommand) -> QueuedCommand:
        # Backpressure: не копим бесконечный хвост при недоступном ПЛК.
        if self._repo.pending_count(channel) >= self._max_pending:
            raise QueueFullError(
                f"Очередь {channel} переполнена (>= {self._max_pending})"
            )
        q = self._repo.enqueue(channel, command)
        self._events.publish(Event.enqueued(q))
        return q

    def pending_count(self, channel: str) -> int:
        return self._repo.pending_count(channel)

    def history(self, channel: str, limit: int = 100):
        return self._repo.history(channel, limit)

    def dead_letters(self, channel: str, limit: int = 100):
        return self._repo.dead_letters(channel, limit)
