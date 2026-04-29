"""Per-guild config cache.

Currently stores only `prefix`. Future per-guild settings should pile in here
so we have one in-memory cache rather than N round-trips per message.
"""
from __future__ import annotations

from typing import Optional

from .database import Database


class GuildConfig:
    def __init__(self, db: Database, default_prefix: str):
        self.db = db
        self.default_prefix = default_prefix
        self._prefix_cache: dict[int, str] = {}

    async def load(self) -> None:
        """Warm the prefix cache from the database."""
        rows = await self.db.fetch(
            "SELECT guild_id, prefix FROM guild_config WHERE prefix IS NOT NULL"
        )
        self._prefix_cache = {r["guild_id"]: r["prefix"] for r in rows}

    def get_prefix(self, guild_id: Optional[int]) -> str:
        if guild_id is None:
            return self.default_prefix
        return self._prefix_cache.get(guild_id, self.default_prefix)

    async def set_prefix(self, guild_id: int, prefix: str) -> None:
        await self.db.execute(
            """
            INSERT INTO guild_config (guild_id, prefix)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET prefix = EXCLUDED.prefix
            """,
            guild_id,
            prefix,
        )
        self._prefix_cache[guild_id] = prefix

    async def reset_prefix(self, guild_id: int) -> None:
        await self.db.execute(
            "UPDATE guild_config SET prefix = NULL WHERE guild_id = $1",
            guild_id,
        )
        self._prefix_cache.pop(guild_id, None)
