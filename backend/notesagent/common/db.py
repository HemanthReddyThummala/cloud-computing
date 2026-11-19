"""PostgreSQL connection pool (psycopg 3, async)."""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from notesagent.common.config import Settings


async def open_pool(settings: Settings, *, name: str, min_size: int = 1, max_size: int = 5,
                    wait: bool = True, timeout: float = 30.0) -> AsyncConnectionPool:
    """Open a pool of autocommit connections returning rows as dicts.

    ``wait=False`` lets a service start while the database is still down; requests then
    fail fast with a pool timeout and /healthz reports the database as unavailable.
    """
    pool = AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        min_size=min_size,
        max_size=max_size,
        open=False,
        name=name,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open(wait=wait, timeout=timeout)
    return pool
