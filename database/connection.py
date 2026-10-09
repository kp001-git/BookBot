import os
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional, Any
import aiosqlite

try:
    import asyncpg
except ImportError:
    asyncpg = None

logger = logging.getLogger(__name__)

_DB_PATH: str = "data/library.db"
_DATABASE_URL: Optional[str] = None
_PG_POOL: Optional[Any] = None


def normalize_postgres_url_for_asyncpg(url: str) -> str:
    """Normalizes Postgres URL to standard postgresql:// scheme for asyncpg."""
    if not url:
        return ""
    clean = url.strip()
    if clean.startswith("postgresql+asyncpg://"):
        return "postgresql://" + clean[len("postgresql+asyncpg://"):]
    if clean.startswith("postgres://"):
        return "postgresql://" + clean[len("postgres://"):]
    return clean


def to_asyncpg_scheme(url: str) -> str:
    """Converts standard postgres/postgresql scheme to postgresql+asyncpg:// scheme."""
    if not url:
        return ""
    clean = url.strip()
    if clean.startswith("postgres://"):
        return "postgresql+asyncpg://" + clean[len("postgres://"):]
    if clean.startswith("postgresql://") and not clean.startswith("postgresql+asyncpg://"):
        return "postgresql+asyncpg://" + clean[len("postgresql://"):]
    return clean


def set_database_url(url: Optional[str]) -> None:
    """Sets PostgreSQL connection string."""
    global _DATABASE_URL, _PG_POOL
    _DATABASE_URL = url.strip() if url and url.strip() else None
    _PG_POOL = None


def get_database_url() -> Optional[str]:
    """Returns current DATABASE_URL if configured."""
    return _DATABASE_URL


def is_postgres() -> bool:
    """Returns True if PostgreSQL mode is active."""
    return bool(_DATABASE_URL)


def get_db_engine_name() -> str:
    """Returns human-readable name of active database engine."""
    return "PostgreSQL" if is_postgres() else "SQLite FTS5"


def set_db_path(db_path: str) -> None:
    """Sets SQLite file path and disables postgres mode."""
    global _DB_PATH, _DATABASE_URL, _PG_POOL
    _DB_PATH = db_path
    _DATABASE_URL = None
    _PG_POOL = None
    db_dir = os.path.dirname(os.path.abspath(db_path))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)


def get_db_path() -> str:
    """Returns the current SQLite file path."""
    return _DB_PATH


async def get_pg_pool() -> Any:
    """Retrieves or creates asyncpg connection pool."""
    global _PG_POOL
    if _PG_POOL is None:
        if asyncpg is None:
            raise RuntimeError("asyncpg is required for PostgreSQL mode. Please install asyncpg.")
        if not _DATABASE_URL:
            raise ValueError("DATABASE_URL is not set.")
        dsn = normalize_postgres_url_for_asyncpg(_DATABASE_URL)
        _PG_POOL = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
    return _PG_POOL


async def close_db() -> None:
    """Closes connection pools cleanly upon shutdown."""
    global _PG_POOL
    if _PG_POOL is not None:
        await _PG_POOL.close()
        _PG_POOL = None
        logger.info("PostgreSQL connection pool closed.")


@asynccontextmanager
async def get_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    Yields an aiosqlite Connection configured with WAL mode, foreign keys,
    and performance-optimized PRAGMAs. Used in SQLite mode.
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
