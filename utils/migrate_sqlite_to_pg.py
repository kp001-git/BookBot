"""
utils/migrate_sqlite_to_pg.py
One-off migration script to stream the complete local SQLite library (data/library.db)
directly into Neon PostgreSQL via asyncpg.copy_records_to_table (binary COPY protocol).
"""

import os
import sys
import time
import argparse
import asyncio
import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Tuple, Any

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from config import config
from database.connection import set_database_url, normalize_postgres_url_for_asyncpg
from database.models import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("migration")


def parse_timestamp(val: Any) -> Optional[datetime]:
    """Converts SQLite ISO/text timestamp strings into Python datetime objects for asyncpg COPY binary encoder."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


async def run_migration(
    sqlite_path: str = "data/library.db",
    database_url: Optional[str] = None,
    clean: bool = True
) -> dict:
    """
    Reads rows from SQLite and streams them into PostgreSQL using asyncpg binary COPY.
    
    Args:
        sqlite_path: Path to local SQLite database file.
        database_url: Target PostgreSQL / Neon connection string.
        clean: If True, truncates destination tables first for an exact 1:1 match.
               If False, uses temporary staging tables with ON CONFLICT resolution.
    """
    t_start = time.perf_counter()

    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite database file not found at: {sqlite_path}")

    target_url = database_url or config.DATABASE_URL or os.environ.get("DATABASE_URL")
    if not target_url:
        raise ValueError(
            "Target DATABASE_URL is not set. Provide it via --database-url, DATABASE_URL in .env, or environment."
        )

    clean_dsn = normalize_postgres_url_for_asyncpg(target_url)

    logger.info("=" * 65)
    logger.info("🚀 Starting SQLite -> Neon PostgreSQL Direct Migration")
    logger.info(f"Source SQLite:   {sqlite_path}")
    logger.info(f"Target Postgres: {clean_dsn.split('@')[-1] if '@' in clean_dsn else 'PostgreSQL'}")
    logger.info(f"Migration Mode:  {'Clean Truncate & Mirror (1:1)' if clean else 'Staging Merge (ON CONFLICT)'}")
    logger.info("=" * 65)

    # 1. Read SQLite records in memory
    logger.info("Reading source SQLite database...")
    s_conn = sqlite3.connect(sqlite_path)
    s_conn.row_factory = sqlite3.Row
    s_cur = s_conn.cursor()

    # Read Books
    s_cur.execute("""
        SELECT id, clean_title, clean_author, canonical_title, canonical_author,
               cover_url, description, rating, genres, year, created_at
        FROM books
        ORDER BY id
    """)
    book_rows = [
        (
            r["id"],
            r["clean_title"],
            r["clean_author"],
            r["canonical_title"],
            r["canonical_author"],
            r["cover_url"],
            r["description"],
            r["rating"],
            r["genres"],
            r["year"],
            parse_timestamp(r["created_at"])
        )
        for r in s_cur.fetchall()
    ]
    logger.info(f"Loaded {len(book_rows):,} books from SQLite.")

    # Read Book Files
    s_cur.execute("""
        SELECT id, book_id, format, file_id, file_unique_id, channel_message_id,
               file_size, file_name, duration, created_at
        FROM book_files
        ORDER BY id
    """)
    file_rows = [
        (
            r["id"],
            r["book_id"],
            r["format"],
            r["file_id"],
            r["file_unique_id"],
            r["channel_message_id"],
            r["file_size"],
            r["file_name"],
            r["duration"],
            parse_timestamp(r["created_at"])
        )
        for r in s_cur.fetchall()
    ]
    logger.info(f"Loaded {len(file_rows):,} book_files from SQLite.")

    # Read Users (if table exists)
    user_rows = []
    try:
        s_cur.execute("SELECT user_id, username, first_seen, total_downloads FROM users ORDER BY user_id")
        user_rows = [
            (
                r["user_id"],
                r["username"],
                parse_timestamp(r["first_seen"]),
                r["total_downloads"]
            )
            for r in s_cur.fetchall()
        ]
        logger.info(f"Loaded {len(user_rows):,} users from SQLite.")
    except Exception as e:
        logger.debug(f"Users table read note: {e}")

    s_conn.close()

    # 2. Initialize PostgreSQL schema
    logger.info("Initializing PostgreSQL schema and indices...")
    set_database_url(target_url)
    await init_db()

    # 3. Stream data via asyncpg binary COPY
    logger.info("Connecting to PostgreSQL and executing binary COPY...")
    pg_conn = await asyncpg.connect(clean_dsn)

    book_columns = [
        "id", "clean_title", "clean_author", "canonical_title", "canonical_author",
        "cover_url", "description", "rating", "genres", "year", "created_at"
    ]
    file_columns = [
        "id", "book_id", "format", "file_id", "file_unique_id", "channel_message_id",
        "file_size", "file_name", "duration", "created_at"
    ]
    user_columns = ["user_id", "username", "first_seen", "total_downloads"]

    try:
        async with pg_conn.transaction():
            if clean:
                logger.info("Truncating destination tables with CASCADE...")
                await pg_conn.execute("TRUNCATE TABLE book_files, books RESTART IDENTITY CASCADE;")

                logger.info(f"Streaming {len(book_rows):,} books into PostgreSQL via binary COPY...")
                await pg_conn.copy_records_to_table("books", records=book_rows, columns=book_columns)

                logger.info(f"Streaming {len(file_rows):,} book_files into PostgreSQL via binary COPY...")
                await pg_conn.copy_records_to_table("book_files", records=file_rows, columns=file_columns)

                if user_rows:
                    logger.info(f"Streaming {len(user_rows):,} users into PostgreSQL...")
                    await pg_conn.execute("TRUNCATE TABLE users RESTART IDENTITY;")
                    await pg_conn.copy_records_to_table("users", records=user_rows, columns=user_columns)
            else:
                logger.info("Using staging tables with ON CONFLICT merge...")
                # Staging Books
                await pg_conn.execute("""
                    CREATE TEMP TABLE stage_books (
                        id INTEGER,
                        clean_title TEXT,
                        clean_author TEXT,
                        canonical_title TEXT,
                        canonical_author TEXT,
                        cover_url TEXT,
                        description TEXT,
                        rating REAL,
                        genres TEXT,
                        year TEXT,
                        created_at TIMESTAMP
                    );
                """)
                await pg_conn.copy_records_to_table("stage_books", records=book_rows, columns=book_columns)

                await pg_conn.execute("""
                    INSERT INTO books (id, clean_title, clean_author, canonical_title, canonical_author, cover_url, description, rating, genres, year, created_at)
                    SELECT id, clean_title, clean_author, canonical_title, canonical_author, cover_url, description, rating, genres, year, created_at
                    FROM stage_books
                    ON CONFLICT (clean_title, clean_author) DO UPDATE SET
                        canonical_title = COALESCE(EXCLUDED.canonical_title, books.canonical_title),
                        canonical_author = COALESCE(EXCLUDED.canonical_author, books.canonical_author),
                        cover_url = COALESCE(EXCLUDED.cover_url, books.cover_url),
                        description = COALESCE(EXCLUDED.description, books.description),
                        rating = COALESCE(EXCLUDED.rating, books.rating),
                        genres = COALESCE(EXCLUDED.genres, books.genres),
                        year = COALESCE(EXCLUDED.year, books.year);
                """)

                # Staging Book Files
                await pg_conn.execute("""
                    CREATE TEMP TABLE stage_book_files (
                        id INTEGER,
                        book_id INTEGER,
                        format TEXT,
                        file_id TEXT,
                        file_unique_id TEXT,
                        channel_message_id INTEGER,
                        file_size BIGINT,
                        file_name TEXT,
                        duration INTEGER,
                        created_at TIMESTAMP
                    );
                """)
                await pg_conn.copy_records_to_table("stage_book_files", records=file_rows, columns=file_columns)

                await pg_conn.execute("""
                    INSERT INTO book_files (id, book_id, format, file_id, file_unique_id, channel_message_id, file_size, file_name, duration, created_at)
                    SELECT id, book_id, format, file_id, file_unique_id, channel_message_id, file_size, file_name, duration, created_at
                    FROM stage_book_files
                    ON CONFLICT (file_id) DO UPDATE SET
                        channel_message_id = COALESCE(EXCLUDED.channel_message_id, book_files.channel_message_id),
                        file_unique_id = COALESCE(EXCLUDED.file_unique_id, book_files.file_unique_id),
                        file_size = COALESCE(EXCLUDED.file_size, book_files.file_size),
                        file_name = COALESCE(EXCLUDED.file_name, book_files.file_name),
                        duration = COALESCE(EXCLUDED.duration, book_files.duration);
                """)

                if user_rows:
                    await pg_conn.execute("""
                        CREATE TEMP TABLE stage_users (
                            user_id BIGINT,
                            username TEXT,
                            first_seen TIMESTAMP,
                            total_downloads INTEGER
                        );
                    """)
                    await pg_conn.copy_records_to_table("stage_users", records=user_rows, columns=user_columns)
                    await pg_conn.execute("""
                        INSERT INTO users (user_id, username, first_seen, total_downloads)
                        SELECT user_id, username, first_seen, total_downloads
                        FROM stage_users
                        ON CONFLICT (user_id) DO UPDATE SET
                            username = EXCLUDED.username,
                            total_downloads = GREATEST(users.total_downloads, EXCLUDED.total_downloads);
                    """)

            # 4. Synchronize auto-increment sequences
            logger.info("Synchronizing PostgreSQL serial sequences...")
            await pg_conn.execute("SELECT setval('books_id_seq', COALESCE((SELECT MAX(id) FROM books), 1));")
            await pg_conn.execute("SELECT setval('book_files_id_seq', COALESCE((SELECT MAX(id) FROM book_files), 1));")

        # 5. Verify post-migration counts
        pg_books = await pg_conn.fetchval("SELECT COUNT(*) FROM books;")
        pg_files = await pg_conn.fetchval("SELECT COUNT(*) FROM book_files;")
        pg_users = await pg_conn.fetchval("SELECT COUNT(*) FROM users;")

    finally:
        await pg_conn.close()

    t_elapsed = time.perf_counter() - t_start
    total_rows = len(book_rows) + len(file_rows) + len(user_rows)
    rate = total_rows / t_elapsed if t_elapsed > 0 else 0

    metrics = {
        "books_migrated": len(book_rows),
        "files_migrated": len(file_rows),
        "users_migrated": len(user_rows),
        "total_rows": total_rows,
        "pg_books_total": pg_books,
        "pg_files_total": pg_files,
        "pg_users_total": pg_users,
        "elapsed_seconds": t_elapsed,
        "rows_per_second": rate
    }

    logger.info("=" * 65)
    logger.info("🎉 SQLite -> PostgreSQL Migration Completed Successfully!")
    logger.info(f"Books in Postgres:      {pg_books:,} (migrated: {len(book_rows):,})")
    logger.info(f"Files in Postgres:      {pg_files:,} (migrated: {len(file_rows):,})")
    logger.info(f"Users in Postgres:      {pg_users:,} (migrated: {len(user_rows):,})")
    logger.info(f"Total rows transferred: {total_rows:,}")
    logger.info(f"Total time elapsed:     {t_elapsed:.2f}s ({rate:,.1f} rows/sec)")
    logger.info("=" * 65)
    return metrics


def main():
    parser = argparse.ArgumentParser(
        description="Fast direct binary migration of local SQLite (data/library.db) into Neon PostgreSQL."
    )
    parser.add_argument(
        "--sqlite-path",
        type=str,
        default="data/library.db",
        help="Path to local SQLite database (default: 'data/library.db')"
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Target Neon / PostgreSQL connection string (defaults to DATABASE_URL in .env)"
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        default=True,
        help="Truncate target Postgres tables before copying for an exact 1:1 mirror (default: True)"
    )
    parser.add_argument(
        "--merge",
        dest="clean",
        action="store_false",
        help="Do not truncate; merge rows into target Postgres using staging tables and ON CONFLICT"
    )

    args = parser.parse_args()

    asyncio.run(
        run_migration(
            sqlite_path=args.sqlite_path,
            database_url=args.database_url,
            clean=args.clean
        )
    )


if __name__ == "__main__":
    main()
