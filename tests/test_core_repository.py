"""Транзакционная очередь + история + DLQ + seq (core.repository)."""
from __future__ import annotations

import pytest

from core.database import Database
from core.events import EventBus, EventType
from core.models import CommandState, DeadLetterReason, MotionCommand
from core.repository import CommandRepository
from core.service import CommandService, QueueFullError


def _cmd(pos: int = 100) -> MotionCommand:
    return MotionCommand(target_position=pos, speed=10, command_code=1)


@pytest.fixture
def repo(tmp_path):
    return CommandRepository(Database(tmp_path / "q.db"))


def _history_status(repo, channel="A"):
    return {r["seq"]: r["status"] for r in repo.history(channel)}


def test_seq_is_monotonic_per_channel(repo):
    assert [repo.enqueue("A", _cmd()).seq for _ in range(3)] == [1, 2, 3]
    assert repo.enqueue("B", _cmd()).seq == 1
    assert repo.enqueue("A", _cmd()).seq == 4


def test_seq_survives_restart(tmp_path):
    path = tmp_path / "q.db"
    r1 = CommandRepository(Database(path))
    q = r1.enqueue("A", _cmd())
    r1.complete(r1.claim_next("A"))
    r2 = CommandRepository(Database(path))
    assert r2.enqueue("A", _cmd()).seq == q.seq + 1


def test_claim_is_fifo_and_marks_in_flight(repo):
    first = repo.enqueue("A", _cmd(1))
    repo.enqueue("A", _cmd(2))
    claimed = repo.claim_next("A")
    assert claimed.seq == first.seq
    assert claimed.state is CommandState.IN_FLIGHT
    assert claimed.command == _cmd(1)
    assert repo.pending_count("A") == 1
    assert [q.seq for q in repo.resume_in_flight("A")] == [first.seq]
    assert _history_status(repo)[first.seq] == "IN_FLIGHT"


def test_claim_empty_returns_none(repo):
    assert repo.claim_next("A") is None


def test_complete_removes_and_records_done(repo):
    repo.enqueue("A", _cmd())
    q = repo.claim_next("A")
    repo.complete(q)
    assert repo.resume_in_flight("A") == []
    assert repo.pending_count("A") == 0
    assert _history_status(repo) == {q.seq: "DONE"}


def test_dead_letter_is_atomic_with_history(repo):
    repo.enqueue("A", _cmd())
    q = repo.claim_next("A")
    repo.dead_letter(q, DeadLetterReason.PLC_ERROR, "boom")
    assert repo.resume_in_flight("A") == []
    [dl] = repo.dead_letters("A")
    assert (dl["seq"], dl["reason"], dl["error_text"]) == (q.seq, "PLC_ERROR", "boom")
    assert _history_status(repo) == {q.seq: "FAILED"}


def test_mark_retry_returns_to_pending(repo):
    repo.enqueue("A", _cmd())
    q = repo.claim_next("A")
    assert repo.mark_retry(q) == 1
    again = repo.claim_next("A")
    assert again.seq == q.seq and again.attempts == 1


def test_failed_transaction_rolls_back(tmp_path):
    db = Database(tmp_path / "t.db")
    r = CommandRepository(db)
    r.enqueue("A", _cmd())
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("DELETE FROM command_queue;")
            raise RuntimeError("crash mid-transaction")
    assert r.pending_count("A") == 1


def test_service_backpressure_and_event(repo):
    events = EventBus()
    got = []
    events.subscribe(got.append)
    svc = CommandService(repo, events, max_pending=2)
    svc.submit("A", _cmd())
    svc.submit("A", _cmd())
    with pytest.raises(QueueFullError):
        svc.submit("A", _cmd())
    assert [e.type for e in got] == [EventType.ENQUEUED, EventType.ENQUEUED]


def test_event_bus_swallows_subscriber_errors():
    bus = EventBus()
    got = []

    def bad(_e):
        raise ValueError("subscriber bug")

    bus.subscribe(bad)
    bus.subscribe(got.append)
    from core.events import Event
    bus.publish(Event(EventType.DONE, "A", 1))
    assert len(got) == 1
