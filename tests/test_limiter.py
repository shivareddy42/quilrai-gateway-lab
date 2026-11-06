import asyncio
import multiprocessing
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing

import pytest

from quilr_gateway.limiter import LimitExceeded, SlidingWindow


async def retry_busy(operation, *, retry_budget=5.0):
    """Test-client retry budget for quota correctness, not an admission latency SLA.

    A busy CI worker may correctly hit production's 250ms fail-closed limit.
    Retry only SQLite BUSY; corruption, I/O faults, and quota denial must escape.
    """
    deadline = time.monotonic() + retry_budget
    while True:
        try:
            return await operation()
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", -1)
            if code & 0xFF != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.01)


async def test_exact_cutoff_restart_and_tenant_isolation(tmp_path):
    clock = [100.0]
    path = str(tmp_path / "quota.sqlite3")
    limiter = SlidingWindow(path, limit=10, clock=lambda: clock[0])
    reservation = await limiter.reserve("tenant-a", 10)
    await limiter.settle(reservation, 10)
    with pytest.raises(LimitExceeded):
        await limiter.reserve("tenant-a", 1)
    await limiter.reserve("tenant-b", 10)
    clock[0] = 159.999
    restarted = SlidingWindow(path, limit=10, clock=lambda: clock[0])
    with pytest.raises(LimitExceeded) as error:
        await restarted.reserve("tenant-a", 1)
    assert error.value.retry_after == 1
    clock[0] = 160.0
    await restarted.reserve("tenant-a", 10)


async def test_actual_usage_refund_idempotence_and_crash_lease(tmp_path):
    clock = [100.0]
    limiter = SlidingWindow(str(tmp_path / "quota.sqlite3"), limit=10, clock=lambda: clock[0])
    first = await limiter.reserve("a", 10)
    await limiter.settle(first, 3)
    await limiter.settle(first, 0)  # Duplicate settlement cannot credit again.
    pending = await limiter.reserve("a", 7)
    with pytest.raises(LimitExceeded):
        await limiter.reserve("a", 1)
    clock[0] = 161
    with pytest.raises(LimitExceeded):
        await limiter.reserve("a", 4)  # Pending crash lease is still charged.
    clock[0] = 190
    await limiter.reserve("a", 10)
    assert pending.id != first.id


async def test_same_tenant_concurrency_multiple_connections(tmp_path):
    path = str(tmp_path / "quota.sqlite3")
    limiters = [SlidingWindow(path, limit=50_000, clock=lambda: 100.0) for _ in range(8)]

    async def attempt(i):
        try:
            await retry_busy(lambda: limiters[i % 8].reserve("a", 1000))
            return True
        except LimitExceeded:
            return False

    results = await asyncio.gather(*(attempt(i) for i in range(80)))
    assert sum(results) == 50
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("SELECT SUM(tokens) FROM ledger").fetchone()[0] == 50_000
        assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 50
        assert db.execute("SELECT tenant FROM ledger LIMIT 1").fetchone()[0] != "a"


def process_reserve(path):
    async def attempt():
        limiter = SlidingWindow(path, limit=10, clock=lambda: 100.0)
        return await limiter.reserve("a", 1)

    try:
        asyncio.run(retry_busy(attempt))
        return True
    except LimitExceeded:
        return False


@pytest.mark.parametrize("start_method", multiprocessing.get_all_start_methods())
def test_real_processes_share_atomic_quota(tmp_path, start_method):
    path = str(tmp_path / "quota.sqlite3")
    SlidingWindow(path, limit=10)
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context(start_method)
    ) as pool:
        results = list(pool.map(process_reserve, [path] * 20))
    assert sum(results) == 10
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("SELECT COUNT(*), SUM(tokens) FROM ledger").fetchone() == (10, 10)


async def test_clock_rollback_cannot_expire_state_early(tmp_path):
    clock = [100.0]
    limiter = SlidingWindow(str(tmp_path / "quota.sqlite3"), limit=10, clock=lambda: clock[0])
    reservation = await limiter.reserve("a", 10)
    clock[0] = 50
    await limiter.settle(reservation, 10)
    clock[0] = 159
    with pytest.raises(LimitExceeded):
        await limiter.reserve("a", 1)
    clock[0] = 160
    await limiter.reserve("a", 10)


async def test_storage_lock_fails_closed(tmp_path):
    path = str(tmp_path / "quota.sqlite3")
    limiter = SlidingWindow(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as error:
            await limiter.reserve("a", 1)
        assert error.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


async def test_contention_retry_after_busy_does_not_double_charge(tmp_path):
    path = str(tmp_path / "quota.sqlite3")
    limiter = SlidingWindow(path, limit=10)
    attempts = 0
    with closing(sqlite3.connect(path)) as blocker, blocker:
        blocker.execute("BEGIN IMMEDIATE")

        async def attempt():
            nonlocal attempts
            attempts += 1
            try:
                return await limiter.reserve("a", 10)
            except sqlite3.OperationalError:
                # Release only after the first real lock timeout, without a sleep race.
                blocker.rollback()
                raise

        await retry_busy(attempt)
        assert attempts == 2
        assert blocker.execute("SELECT COUNT(*), SUM(tokens) FROM ledger").fetchone() == (1, 10)
    with pytest.raises(LimitExceeded):
        await limiter.reserve("a", 1)


@pytest.mark.parametrize(
    "code", [sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_LOCKED]
)
async def test_contention_retry_never_hides_other_database_failures(code):
    attempts = 0
    failure = sqlite3.OperationalError("Injected non-BUSY database failure")
    failure.sqlite_errorcode = code

    async def attempt():
        nonlocal attempts
        attempts += 1
        raise failure

    with pytest.raises(sqlite3.OperationalError) as error:
        await retry_busy(attempt)
    assert error.value is failure and attempts == 1


async def test_contention_retry_stops_at_its_deadline(tmp_path):
    path = str(tmp_path / "quota.sqlite3")
    limiter = SlidingWindow(path)
    with closing(sqlite3.connect(path)) as blocker, blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as error:
            await retry_busy(lambda: limiter.reserve("a", 1), retry_budget=0)
        assert error.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert blocker.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


@pytest.mark.parametrize("tokens", [0, -1, True, 1.5])
async def test_invalid_reservation(tmp_path, tokens):
    limiter = SlidingWindow(str(tmp_path / "quota.sqlite3"))
    with pytest.raises(ValueError):
        await limiter.reserve("a", tokens)


async def test_connections_close_after_initialization_success_and_failure(tmp_path, monkeypatch):
    # Retain handles so garbage collection cannot hide an unclosed connection.
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    def connect(limiter):
        connection = sqlite3.connect(limiter.path, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(SlidingWindow, "_connect", connect)
    limiter = SlidingWindow(str(tmp_path / "quota.sqlite3"), limit=10)
    assert connections[-1].closed
    reservation = await limiter.reserve("a", 10)
    assert connections[-1].closed
    with pytest.raises(LimitExceeded):
        await limiter.reserve("a", 1)
    assert connections[-1].closed
    await limiter.settle(reservation, 3)
    assert len(connections) == 4 and all(connection.closed for connection in connections)

    def failed_clock(db):
        raise RuntimeError("Injected transaction failure")

    monkeypatch.setattr(limiter, "_now", failed_clock)
    with pytest.raises(RuntimeError, match="Injected transaction failure"):
        await limiter.reserve("a", 1)
    assert len(connections) == 5 and all(connection.closed for connection in connections)
