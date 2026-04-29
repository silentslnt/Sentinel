"""Postgres connection pool + schema bootstrap.

Uses asyncpg. Connection string comes from $DATABASE_URL (Railway injects this
automatically when you attach a Postgres plugin).
"""
from __future__ import annotations

import os
from typing import Optional

import asyncpg

# Schema is intentionally light in Phase 1 — feature cogs add their own tables
# in their own setup() via Database.execute(SCHEMA_SQL).
CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id BIGINT PRIMARY KEY,
    prefix   TEXT
);
"""


class Database:
    """Thin asyncpg pool wrapper. Singleton per process."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            self.dsn,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
        async with self.pool.acquire() as conn:
            await conn.execute(CORE_SCHEMA)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def execute(self, query: str, *args) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)


def from_env() -> Database:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. On Railway: attach a Postgres plugin to "
            "this service. Locally: set DATABASE_URL in your .env."
        )
    return Database(dsn)
