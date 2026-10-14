"""SQLite storage (aiosqlite): tickets, outbox, idempotency keys, feedback, app secrets.

A single connection in autocommit mode, guarded by an `asyncio.Lock`, is plenty for one API
instance (operations take well under a millisecond). Multi-statement operations use explicit
`BEGIN IMMEDIATE` transactions. For several API replicas, replace this with PostgreSQL - the
repositories are the only code that knows SQL.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    ticket_code TEXT NOT NULL,
    request_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    sender_verification TEXT NOT NULL,
    message TEXT,
    result TEXT,
    error TEXT,
    department TEXT,
    routed_by TEXT,
    prompt_version TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets (status, created_at);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    record TEXT NOT NULL,
    raw BLOB NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_outbox_ticket ON outbox (ticket_id);

CREATE TABLE IF NOT EXISTS idempotency (
    scope_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    status_code INTEGER,
    body TEXT,
    headers TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    message TEXT NOT NULL,
    routed_department TEXT NOT NULL,
    correct_department TEXT NOT NULL,
    comment TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_secrets (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class StorageError(Exception):
    """The database could not complete an operation."""


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> str:
        return self._path

    async def connect(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        if self._path != ":memory:":
            await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise StorageError("database is not connected")
        return self._conn

    async def _migrate(self) -> None:
        conn = self._connection()
        async with conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        version = int(row[0]) if row else 0
        if version < SCHEMA_VERSION:
            await conn.executescript(_SCHEMA)
            await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run one statement; returns the number of affected rows."""
        async with self._lock:
            try:
                cursor = await self._connection().execute(sql, tuple(params))
                await cursor.close()
                return cursor.rowcount
            except aiosqlite.Error as exc:
                raise StorageError(str(exc)) from exc

    async def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run an INSERT; returns the new row id."""
        async with self._lock:
            try:
                cursor = await self._connection().execute(sql, tuple(params))
                await cursor.close()
                return int(cursor.lastrowid or 0)
            except aiosqlite.Error as exc:
                raise StorageError(str(exc)) from exc

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self._lock:
            try:
                async with self._connection().execute(sql, tuple(params)) as cursor:
                    return await cursor.fetchone()
            except aiosqlite.Error as exc:
                raise StorageError(str(exc)) from exc

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            try:
                async with self._connection().execute(sql, tuple(params)) as cursor:
                    return list(await cursor.fetchall())
            except aiosqlite.Error as exc:
                raise StorageError(str(exc)) from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Exclusive multi-statement transaction (`BEGIN IMMEDIATE ... COMMIT`)."""
        async with self._lock:
            conn = self._connection()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    await conn.execute("ROLLBACK")
                    raise
                await conn.execute("COMMIT")
            except aiosqlite.Error as exc:
                raise StorageError(str(exc)) from exc

    async def ping(self) -> None:
        await self.fetchone("SELECT 1")

    async def get_or_create_secret(self, name: str) -> str:
        """A random secret generated on first use and kept in the database (survives restarts)."""
        async with self.transaction() as conn:
            async with conn.execute("SELECT value FROM app_secrets WHERE name = ?", (name,)) as cur:
                row = await cur.fetchone()
            if row is not None:
                return str(row["value"])
            value = secrets.token_urlsafe(32)
            await conn.execute("INSERT INTO app_secrets (name, value) VALUES (?, ?)", (name, value))
            return value
