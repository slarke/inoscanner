"""Слой БД: один файл SQLite в режиме WAL.

Ключевая идея архитектуры: очередь, история, dead-letter и счётчик seq
лежат в ОДНОЙ базе, поэтому связанные изменения делаются одной транзакцией
(атомарно). Это и есть граница корректности для effectively-once.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_generator (
    channel  TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS command_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    seq        INTEGER NOT NULL,
    channel    TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    state      TEXT    NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    updated_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_channel_state
    ON command_queue(channel, state, id);

CREATE TABLE IF NOT EXISTS command_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    seq          INTEGER NOT NULL,
    channel      TEXT    NOT NULL,
    command_type INTEGER,
    payload      TEXT,
    status       TEXT    NOT NULL,
    error_text   TEXT,
    created_at   REAL,
    started_at   REAL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_history_channel_seq
    ON command_history(channel, seq);

CREATE TABLE IF NOT EXISTS dead_letter (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    seq        INTEGER NOT NULL,
    channel    TEXT    NOT NULL,
    payload    TEXT,
    reason     TEXT    NOT NULL,
    error_text TEXT,
    created_at REAL    NOT NULL
);
"""


class Database:
    """Управление одним SQLite-файлом.

    SQLite-объекты не делятся между потоками, поэтому соединение — per-thread
    (через threading.local). На каждый поток своё соединение в WAL-режиме.
    """

    def __init__(self, path: str | Path):
        self._path = str(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._bootstrap()

    # --- соединения ---
    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._path,
            timeout=30.0,
            isolation_level=None,   # ручное управление транзакциями
            check_same_thread=True,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")   # durability важнее скорости
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Атомарная транзакция: всё внутри коммитится целиком либо откатывается."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    # --- схема и миграции ---
    def _bootstrap(self) -> None:
        with self._init_lock:
            conn = self._new_connection()
            try:
                conn.executescript(_SCHEMA)
                row = conn.execute(
                    "SELECT version FROM schema_version;"
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO schema_version(version) VALUES (?);",
                        (SCHEMA_VERSION,),
                    )
                else:
                    self._migrate(conn, int(row[0]))
                conn.commit()
            finally:
                conn.close()

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Точка расширения для миграций. Сейчас актуальна версия 1."""
        if from_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Схема БД v{from_version} новее кода (v{SCHEMA_VERSION})"
            )
        # Будущие миграции: while from_version < SCHEMA_VERSION: ... ; bump version
