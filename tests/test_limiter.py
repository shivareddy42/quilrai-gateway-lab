import asyncio
import sqlite3
from concurrent.futures import ProcessPoolExecutor

import pytest

from quilr_gateway.limiter import LimitExceeded, SlidingWindow


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
    limiters = [SlidingWindow(path, limit=50_000) for _ in range(8)]

    async def attempt(i):
        try:
            await limiters[i % 8].reserve("a", 1000)
            return True
        except LimitExceeded:
            return False

    results = await asyncio.gather(*(attempt(i) for i in range(80)))
    assert sum(results) == 50
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT SUM(tokens) FROM ledger").fetchone()[0] == 50_000
        assert db.execute("SELECT tenant FROM ledger LIMIT 1").fetchone()[0] != "a"


def process_reserve(path):
    limiter = SlidingWindow(path, limit=10)
    try:
        asyncio.run(limiter.reserve("a", 1))
        return True
    except LimitExceeded:
        return False


def test_real_processes_share_atomic_quota(tmp_path):
    path = str(tmp_path / "quota.sqlite3")
    SlidingWindow(path, limit=10)
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(process_reserve, [path] * 20))
    assert sum(results) == 10


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
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            await limiter.reserve("a", 1)


@pytest.mark.parametrize("tokens", [0, -1, True, 1.5])
async def test_invalid_reservation(tmp_path, tokens):
    limiter = SlidingWindow(str(tmp_path / "quota.sqlite3"))
    with pytest.raises(ValueError):
        await limiter.reserve("a", tokens)
