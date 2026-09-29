"""DAO над единой SQLite.

Все операции, меняющие очередь, выполняются в ОДНОЙ транзакции вместе с
записью в историю/dead-letter — очередь и аудит не могут рассинхронироваться.
seq монотонный, на канал, переживает рестарт (хранится в БД).
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from .database import Database
from .models import (
    CommandState,
    DeadLetterReason,
    MotionCommand,
    QueuedCommand,
)


class CommandRepository:
    def __init__(self, db: Database):
        self._db = db

    # ------------------------------------------------------------------ #
    # Постановка команды + монотонный seq (атомарно в одной транзакции)
    # ------------------------------------------------------------------ #
    def enqueue(self, channel: str, command: MotionCommand) -> QueuedCommand:
        now = time.time()
        payload = json.dumps(command.to_payload())
        with self._db.transaction() as conn:
            seq = self._bump_seq(conn, channel)
            cur = conn.execute(
                "INSERT INTO command_queue"
                "(seq, channel, payload, state, attempts, created_at, updated_at)"
                " VALUES (?,?,?,?,0,?,?);",
                (seq, channel, payload, CommandState.PENDING.value, now, now),
            )
            qid = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO command_history"
                "(seq, channel, command_type, payload, status, created_at)"
                " VALUES (?,?,?,?,?,?);",
                (seq, channel, command.command_code, payload,
                 CommandState.PENDING.value, now),
            )
        return QueuedCommand(
            id=qid, seq=seq, channel=channel, command=command,
            state=CommandState.PENDING, attempts=0, created_at=now,
        )

    @staticmethod
    def _bump_seq(conn: sqlite3.Connection, channel: str) -> int:
        conn.execute(
            "INSERT INTO sequence_generator(channel, last_seq) VALUES (?, 0)"
            " ON CONFLICT(channel) DO NOTHING;",
            (channel,),
        )
        conn.execute(
            "UPDATE sequence_generator SET last_seq = last_seq + 1"
            " WHERE channel = ?;",
            (channel,),
        )
        row = conn.execute(
            "SELECT last_seq FROM sequence_generator WHERE channel = ?;",
            (channel,),
        ).fetchone()
        return int(row["last_seq"])

    # ------------------------------------------------------------------ #
    # Извлечение / восстановление
    # ------------------------------------------------------------------ #
    def claim_next(self, channel: str) -> Optional[QueuedCommand]:
        """Берёт старейшую PENDING-команду и помечает IN_FLIGHT (атомарно)."""
        now = time.time()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM command_queue"
                " WHERE channel = ? AND state = ? ORDER BY id LIMIT 1;",
                (channel, CommandState.PENDING.value),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE command_queue SET state = ?, updated_at = ? WHERE id = ?;",
                (CommandState.IN_FLIGHT.value, now, row["id"]),
            )
            conn.execute(
                "UPDATE command_history SET status = ?, started_at = ?"
                " WHERE seq = ? AND channel = ? AND completed_at IS NULL;",
                (CommandState.IN_FLIGHT.value, now, row["seq"], channel),
            )
        return self._row_to_queued(row, state=CommandState.IN_FLIGHT)

    def resume_in_flight(self, channel: str) -> list[QueuedCommand]:
        """Команды, взятые но не завершённые до краша — для recovery."""
        rows = self._db.conn.execute(
            "SELECT * FROM command_queue"
            " WHERE channel = ? AND state = ? ORDER BY id;",
            (channel, CommandState.IN_FLIGHT.value),
        ).fetchall()
        return [self._row_to_queued(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Завершение команды (всегда транзакционно с историей/DLQ)
    # ------------------------------------------------------------------ #
    def complete(self, q: QueuedCommand) -> None:
        now = time.time()
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM command_queue WHERE id = ?;", (q.id,))
            conn.execute(
                "UPDATE command_history SET status = ?, completed_at = ?"
                " WHERE seq = ? AND channel = ? AND completed_at IS NULL;",
                (CommandState.DONE.value, now, q.seq, q.channel),
            )

    def dead_letter(
        self, q: QueuedCommand, reason: DeadLetterReason, error_text: str = ""
    ) -> None:
        now = time.time()
        payload = json.dumps(q.command.to_payload())
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM command_queue WHERE id = ?;", (q.id,))
            conn.execute(
                "INSERT INTO dead_letter"
                "(seq, channel, payload, reason, error_text, created_at)"
                " VALUES (?,?,?,?,?,?);",
                (q.seq, q.channel, payload, reason.value, error_text, now),
            )
            conn.execute(
                "UPDATE command_history SET status = ?, error_text = ?, completed_at = ?"
                " WHERE seq = ? AND channel = ? AND completed_at IS NULL;",
                (CommandState.FAILED.value, error_text, now, q.seq, q.channel),
            )

    def mark_retry(self, q: QueuedCommand) -> int:
        """Возврат команды в PENDING для повтора, attempts += 1."""
        now = time.time()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE command_queue"
                " SET state = ?, attempts = attempts + 1, updated_at = ?"
                " WHERE id = ?;",
                (CommandState.PENDING.value, now, q.id),
            )
            row = conn.execute(
                "SELECT attempts FROM command_queue WHERE id = ?;", (q.id,)
            ).fetchone()
        return int(row["attempts"]) if row else q.attempts + 1

    # ------------------------------------------------------------------ #
    # Запросы для GUI / backpressure
    # ------------------------------------------------------------------ #
    def pending_count(self, channel: str) -> int:
        row = self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM command_queue"
            " WHERE channel = ? AND state = ?;",
            (channel, CommandState.PENDING.value),
        ).fetchone()
        return int(row["n"])

    def history(self, channel: str, limit: int = 100) -> list[dict]:
        rows = self._db.conn.execute(
            "SELECT * FROM command_history WHERE channel = ?"
            " ORDER BY id DESC LIMIT ?;",
            (channel, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def dead_letters(self, channel: str, limit: int = 100) -> list[dict]:
        rows = self._db.conn.execute(
            "SELECT * FROM dead_letter WHERE channel = ?"
            " ORDER BY id DESC LIMIT ?;",
            (channel, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_queued(
        row: sqlite3.Row, state: Optional[CommandState] = None
    ) -> QueuedCommand:
        return QueuedCommand(
            id=int(row["id"]),
            seq=int(row["seq"]),
            channel=row["channel"],
            command=MotionCommand.from_payload(json.loads(row["payload"])),
            state=state or CommandState(row["state"]),
            attempts=int(row["attempts"]),
            created_at=float(row["created_at"]),
        )
