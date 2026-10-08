import os
import aiosqlite
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

_DB_PATH: str = "data/library.db"


def set_db_path(db_path: str) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    db_dir = os.path.dirname(os.path.abspath(db_path))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)


def get_db_path() -> str:
    return _DB_PATH


@asynccontextmanager
async def get_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    Yields an aiosqlite Connection configured with WAL mode, foreign keys,
    and performance-optimized PRAGMAs.
    """
    conn = await aiosqlite.connect(_DB_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA journal_mode = WAL;")
        await conn.execute("PRAGMA synchronous = NORMAL;")
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute("PRAGMA cache_size = -64000;")
        await conn.execute("PRAGMA temp_store = MEMORY;")
        yield conn
    finally:
        await conn.close()
