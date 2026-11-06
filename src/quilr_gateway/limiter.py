"""Cross-process atomic reservations in an on-disk SQLite sliding window."""

import asyncio
import hashlib
import math
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import NamedTuple


class LimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class Reservation(NamedTuple):
    id: str
    tokens: int


class SlidingWindow:
    def __init__(self, path: str, limit: int = 50_000, clock=time.time) -> None:
        if path == ":memory:" or limit < 1:
            raise ValueError("An on-disk database and a positive limit are required")
        self.path, self.limit, self.clock = path, limit, clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Connection.__exit__ commits/rolls back; it does not close the handle.
        # Close deterministically so later forked workers cannot inherit it.
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ledger (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, tokens INTEGER NOT NULL,
                    expires REAL NOT NULL, pending INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tenant_expiry ON ledger(tenant, expires);
                CREATE INDEX IF NOT EXISTS expiry ON ledger(expires);
                CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY, last REAL NOT NULL);
                INSERT OR IGNORE INTO clock VALUES(1, 0);
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=0.25)

    def _now(self, db: sqlite3.Connection) -> float:
        # A backwards wall-clock jump must never expire quota early.
        now = max(self.clock(), db.execute("SELECT last FROM clock WHERE id=1").fetchone()[0])
        db.execute("UPDATE clock SET last=? WHERE id=1", (now,))
        return now

    def _reserve(self, tenant: str, tokens: int) -> Reservation:
        if type(tokens) is not int or tokens < 1:
            raise ValueError("tokens must be a positive integer")
        tenant_hash = hashlib.sha256(tenant.encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = self._now(db)
            db.execute("DELETE FROM ledger WHERE expires<=?", (now,))
            used, earliest = db.execute(
                "SELECT COALESCE(SUM(tokens),0), MIN(expires) FROM ledger WHERE tenant=?",
                (tenant_hash,),
            ).fetchone()
            if used + tokens > self.limit:
                # Exception rolls back eviction; logical expiry still applies next time.
                raise LimitExceeded(max(1, math.ceil((earliest or now + 60) - now)))
            reservation = Reservation(uuid.uuid4().hex, tokens)
            # Active requests have a <= 6s router deadline. Crash or cancellation
            # leaves a conservative 90s lease, never a free retry.
            db.execute(
                "INSERT INTO ledger VALUES(?,?,?,?,1)",
                (reservation.id, tenant_hash, tokens, now + 90),
            )
            return reservation

    async def reserve(self, tenant: str, tokens: int) -> Reservation:
        return await asyncio.to_thread(self._reserve, tenant, tokens)

    def _settle(self, reservation: Reservation, actual: int) -> None:
        if type(actual) is not int or not 0 <= actual <= reservation.tokens:
            raise ValueError("Usage outside reservation")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = self._now(db)
            # Idempotent, one-way transition. Duplicate cleanup cannot credit twice.
            db.execute(
                "UPDATE ledger SET tokens=?, expires=?, pending=0 WHERE id=? AND pending=1",
                (actual, now + 60, reservation.id),
            )

    async def settle(self, reservation: Reservation, actual: int) -> None:
        await asyncio.to_thread(self._settle, reservation, actual)
