import re
import asyncio
import aiosqlite
import logging
from typing import Dict, List, Optional, Tuple, Any
from config import settings
from database.connection import get_connection, is_postgres, get_pg_pool

logger = logging.getLogger(__name__)


# ==============================================================================
# SQLITE IMPLEMENTATION (MODE B)
# ==============================================================================

async def _sqlite_init_db() -> None:
    """Initializes SQLite tables, virtual tables, triggers, and indices."""
    async with get_connection() as conn:
        # Logical Book Entry
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clean_title TEXT NOT NULL,
                clean_author TEXT DEFAULT 'Unknown Author',
                canonical_title TEXT,
                canonical_author TEXT,
                cover_url TEXT,
                description TEXT,
                rating REAL,
                genres TEXT,
                year TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(clean_title, clean_author)
            );
        """)

        # Individual Media Formats linked to a Book
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS book_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                format TEXT NOT NULL,                  -- 'AUDIO', 'EPUB', 'PDF', 'ZIP'
                file_id TEXT NOT NULL UNIQUE,           -- Telegram file_id for direct delivery
                file_unique_id TEXT,                    -- Telegram file_unique_id for deduplication
                channel_message_id INTEGER,             -- Storage channel message ID for copy_message
                file_size INTEGER,                      -- in bytes
                file_name TEXT,
                duration INTEGER,                       -- in seconds for audiobooks
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );
        """)

        # Database Migrations for existing databases
        try:
            await conn.execute("ALTER TABLE book_files ADD COLUMN channel_message_id INTEGER;")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE book_files ADD COLUMN file_unique_id TEXT;")
        except Exception:
            pass

        # Full Text Search Virtual Table for fast sub-second queries
        await conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
                clean_title,
                clean_author,
                content='books',
                content_rowid='id'
            );
        """)

        # FTS5 Synchronization Triggers
        await conn.execute("""
            CREATE TRIGGER IF NOT EXISTS books_ai AFTER INSERT ON books BEGIN
                INSERT INTO books_fts(rowid, clean_title, clean_author)
                VALUES (new.id, new.clean_title, new.clean_author);
            END;
        """)

        await conn.execute("""
            CREATE TRIGGER IF NOT EXISTS books_ad AFTER DELETE ON books BEGIN
                INSERT INTO books_fts(books_fts, rowid, clean_title, clean_author)
                VALUES ('delete', old.id, old.clean_title, old.clean_author);
            END;
        """)

        await conn.execute("""
            CREATE TRIGGER IF NOT EXISTS books_au AFTER UPDATE ON books BEGIN
                INSERT INTO books_fts(books_fts, rowid, clean_title, clean_author)
                VALUES ('delete', old.id, old.clean_title, old.clean_author);
                INSERT INTO books_fts(rowid, clean_title, clean_author)
                VALUES (new.id, new.clean_title, new.clean_author);
            END;
        """)

        # User Tracking & Metrics
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_downloads INTEGER DEFAULT 0
            );
        """)

        # RBAC Roles table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS roles (
                user_id INTEGER PRIMARY KEY,
                role TEXT DEFAULT 'user'
            );
        """)

        # User Favorites / Bookmarks table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, book_id),
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );
        """)

        # Community Requests table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Indices for optimal lookup
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_books_canonical ON books(canonical_title, canonical_author);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_book_id ON book_files(book_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_format ON book_files(book_id, format);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_msg_id ON book_files(channel_message_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_unique_id ON book_files(file_unique_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_favorites_user_id ON favorites(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);")

        await conn.commit()
        logger.info("SQLite database schema initialized successfully.")


def _sanitize_fts_query(query: str) -> str:
    """Prepares user query for safe FTS5 prefix search."""
    tokens = re.findall(r"\w+", query)
    if not tokens:
        return '""'
    return " ".join(f'"{t}"*' for t in tokens)


async def _sqlite_upsert_book_and_file(
    clean_title: str,
    clean_author: str,
    canonical_title: str,
    canonical_author: str,
    format_type: str,
    file_id: str,
    file_size: Optional[int] = None,
    file_name: Optional[str] = None,
    duration: Optional[int] = None,
    channel_message_id: Optional[int] = None,
    file_unique_id: Optional[str] = None,
    cover_url: Optional[str] = None,
    description: Optional[str] = None
) -> Tuple[int, bool, bool]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id FROM books
            WHERE (canonical_title = ? AND canonical_author = ?)
               OR (clean_title = ? AND clean_author = ?)
            LIMIT 1
            """,
            (canonical_title, canonical_author, clean_title, clean_author)
        )
        row = await cursor.fetchone()

        is_new_book = False
        if row:
            book_id = row["id"]
            if cover_url or description:
                await conn.execute(
                    """
                    UPDATE books
                    SET cover_url = COALESCE(cover_url, ?),
                        description = COALESCE(description, ?)
                    WHERE id = ?
                    """,
                    (cover_url, description, book_id)
                )
        else:
            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO books (clean_title, clean_author, canonical_title, canonical_author, cover_url, description)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (clean_title, clean_author, canonical_title, canonical_author, cover_url, description)
                )
                book_id = cursor.lastrowid
                is_new_book = True
            except aiosqlite.IntegrityError:
                cursor = await conn.execute(
                    "SELECT id FROM books WHERE clean_title = ? AND clean_author = ?",
                    (clean_title, clean_author)
                )
                existing = await cursor.fetchone()
                book_id = existing["id"]

        # Check if file already exists by file_id, file_unique_id, or channel_message_id
        existing_file = None
        if file_id:
            f_cur = await conn.execute("SELECT id, channel_message_id, file_unique_id FROM book_files WHERE file_id = ?", (file_id,))
            existing_file = await f_cur.fetchone()
        if not existing_file and file_unique_id:
            f_cur = await conn.execute("SELECT id, channel_message_id, file_unique_id FROM book_files WHERE file_unique_id = ?", (file_unique_id,))
            existing_file = await f_cur.fetchone()
        if not existing_file and channel_message_id:
            f_cur = await conn.execute("SELECT id, channel_message_id, file_unique_id FROM book_files WHERE channel_message_id = ?", (channel_message_id,))
            existing_file = await f_cur.fetchone()

        is_new_file = False
        if existing_file:
            # Update missing attributes on existing file record
            if channel_message_id or file_unique_id:
                await conn.execute(
                    """
                    UPDATE book_files
                    SET channel_message_id = COALESCE(channel_message_id, ?),
                        file_unique_id = COALESCE(file_unique_id, ?)
                    WHERE id = ?
                    """,
                    (channel_message_id, file_unique_id, existing_file["id"])
                )
        else:
            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO book_files (book_id, format, file_id, file_unique_id, channel_message_id, file_size, file_name, duration)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (book_id, format_type, file_id, file_unique_id, channel_message_id, file_size, file_name, duration)
                )
                is_new_file = True
            except aiosqlite.IntegrityError:
                pass

        await conn.commit()
        return book_id, is_new_book, is_new_file


async def _sqlite_update_book_metadata(
    book_id: int,
    cover_url: Optional[str] = None,
    description: Optional[str] = None,
    rating: Optional[float] = None,
    genres: Optional[str] = None,
    year: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None
) -> None:
    async with get_connection() as conn:
        canonical_title = title.lower().strip() if title else None
        canonical_author = author.lower().strip() if author else None
        await conn.execute(
            """
            UPDATE books
            SET cover_url = COALESCE(?, cover_url),
                description = COALESCE(?, description),
                rating = COALESCE(?, rating),
                genres = COALESCE(?, genres),
                year = COALESCE(?, year),
                clean_title = COALESCE(?, clean_title),
                clean_author = COALESCE(?, clean_author),
                canonical_title = COALESCE(?, canonical_title),
                canonical_author = COALESCE(?, canonical_author)
            WHERE id = ?
            """,
            (cover_url, description, rating, genres, year, title, author, canonical_title, canonical_author, book_id)
        )
        await conn.commit()


async def _sqlite_search_books(query: str, limit: int = 5, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    clean_q = _sanitize_fts_query(query)
    if clean_q == '""':
        return [], 0

    async with get_connection() as conn:
        count_cursor = await conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM books_fts
            WHERE books_fts MATCH ?
            """,
            (clean_q,)
        )
        count_row = await count_cursor.fetchone()
        total = count_row["total"] if count_row else 0
        if total == 0:
            return [], 0

        cursor = await conn.execute(
            """
            SELECT b.id, b.clean_title, b.clean_author, b.cover_url, b.description,
                   b.rating, b.genres, b.year,
                   GROUP_CONCAT(DISTINCT f.format) as formats
            FROM books_fts fts
            JOIN books b ON b.id = fts.rowid
            LEFT JOIN book_files f ON f.book_id = b.id
            WHERE books_fts MATCH ?
            GROUP BY b.id
            ORDER BY rank
            LIMIT ? OFFSET ?
            """,
            (clean_q, limit, offset)
        )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            formats_str = r["formats"] or ""
            formats_list = [fmt.strip() for fmt in formats_str.split(",") if fmt.strip()]
            results.append({
                "id": r["id"],
                "clean_title": r["clean_title"],
                "clean_author": r["clean_author"],
                "cover_url": r["cover_url"],
                "description": r["description"],
                "rating": r["rating"],
                "genres": r["genres"],
                "year": r["year"],
                "formats": formats_list,
            })
        return results, total


async def _sqlite_fuzzy_search_books(query: str, limit: int = 5, score_cutoff: float = 65.0) -> Tuple[List[Dict[str, Any]], int]:
    """Fallback fuzzy search over recent books using rapidfuzz token_set_ratio."""
    try:
        from rapidfuzz import process, fuzz
    except ImportError:
        logger.warning("rapidfuzz not available; fuzzy fallback skipped.")
        return [], 0

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT b.id, b.clean_title, b.clean_author, b.cover_url, b.description,
                   b.rating, b.genres, b.year,
                   GROUP_CONCAT(DISTINCT f.format) as formats
            FROM books b
            LEFT JOIN book_files f ON f.book_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT 500
            """
        )
        rows = await cursor.fetchall()
        if not rows:
            return [], 0

        candidates = []
        clean_titles = []
        for r in rows:
            formats_str = r["formats"] or ""
            formats_list = [fmt.strip() for fmt in formats_str.split(",") if fmt.strip()]
            candidates.append({
                "id": r["id"],
                "clean_title": r["clean_title"],
                "clean_author": r["clean_author"],
                "cover_url": r["cover_url"],
                "description": r["description"],
                "rating": r["rating"],
                "genres": r["genres"],
                "year": r["year"],
                "formats": formats_list,
            })
            clean_titles.append(r["clean_title"])

        matches = await asyncio.to_thread(
            process.extract,
            query,
            clean_titles,
            scorer=fuzz.token_set_ratio,
            score_cutoff=score_cutoff,
            limit=limit
        )

        if not matches:
            full_strings = [f"{c['clean_title']} {c['clean_author']}".strip() for c in candidates]
            matches = await asyncio.to_thread(
                process.extract,
                query,
                full_strings,
                scorer=fuzz.token_set_ratio,
                score_cutoff=score_cutoff,
                limit=limit
            )

        matched_books = [candidates[idx] for _, score, idx in matches]
        return matched_books, len(matched_books)


async def _sqlite_get_book_by_id(book_id: int) -> Optional[Dict[str, Any]]:
    async with get_connection() as conn:
        book_cursor = await conn.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,)
        )
        book_row = await book_cursor.fetchone()
        if not book_row:
            return None

        files_cursor = await conn.execute(
            """
            SELECT id, format, file_id, file_unique_id, channel_message_id, file_size, file_name, duration
            FROM book_files
            WHERE book_id = ?
            ORDER BY format ASC
            """,
            (book_id,)
        )
        files = await files_cursor.fetchall()

        file_list = [
            {
                "id": f["id"],
                "format": f["format"],
                "file_id": f["file_id"],
                "file_unique_id": f["file_unique_id"],
                "channel_message_id": f["channel_message_id"],
                "file_size": f["file_size"],
                "file_name": f["file_name"],
                "duration": f["duration"]
            }
            for f in files
        ]

        return {
            "id": book_row["id"],
            "clean_title": book_row["clean_title"],
            "clean_author": book_row["clean_author"],
            "cover_url": book_row["cover_url"],
            "description": book_row["description"],
            "rating": book_row["rating"],
            "genres": book_row["genres"],
            "year": book_row["year"],
            "files": file_list
        }


async def _sqlite_get_file_by_id(file_id: int) -> Optional[Dict[str, Any]]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT f.*, b.clean_title, b.clean_author
            FROM book_files f
            JOIN books b ON b.id = f.book_id
            WHERE f.id = ?
            """,
            (file_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return dict(row)


async def _sqlite_register_or_update_user(user_id: int, username: Optional[str] = None) -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username)
            """,
            (user_id, username)
        )
        await conn.commit()


async def _sqlite_increment_user_downloads(user_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, total_downloads)
            VALUES (?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                total_downloads = users.total_downloads + 1
            """,
            (user_id,)
        )
        await conn.commit()


async def _sqlite_get_stats() -> Dict[str, Any]:
    async with get_connection() as conn:
        b_cur = await conn.execute("SELECT COUNT(*) AS total FROM books")
        total_books = (await b_cur.fetchone())["total"]

        f_cur = await conn.execute(
            """
            SELECT format, COUNT(*) AS count
            FROM book_files
            GROUP BY format
            """
        )
        format_counts = {row["format"]: row["count"] for row in await f_cur.fetchall()}

        u_cur = await conn.execute("SELECT COUNT(*) AS total FROM users")
        total_users = (await u_cur.fetchone())["total"]

        d_cur = await conn.execute("SELECT SUM(total_downloads) AS total FROM users")
        row = await d_cur.fetchone()
        total_downloads = row["total"] if row and row["total"] is not None else 0

        return {
            "total_books": total_books,
            "format_counts": format_counts,
            "total_users": total_users,
            "total_downloads": total_downloads,
        }


async def _sqlite_get_all_catalog_books() -> List[Dict[str, Any]]:
    """Loads all books with formats for in-memory cache preloading."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT b.id, b.clean_title, b.clean_author, b.cover_url, b.description,
                   b.rating, b.genres, b.year,
                   GROUP_CONCAT(DISTINCT f.format) as formats
            FROM books b
            LEFT JOIN book_files f ON f.book_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
            """
        )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            formats_str = r["formats"] or ""
            formats_list = [fmt.strip() for fmt in formats_str.split(",") if fmt.strip()]
            results.append({
                "id": r["id"],
                "clean_title": r["clean_title"],
                "clean_author": r["clean_author"] or "Unknown Author",
                "cover_url": r["cover_url"],
                "description": r["description"],
                "rating": r["rating"],
                "genres": r["genres"],
                "year": r["year"],
                "formats": formats_list,
            })
        return results


async def _sqlite_get_user_role(user_id: int) -> str:
    """Returns the role of a user from SQLite ('admin', 'moderator', 'user')."""
    if user_id in settings.ADMIN_IDS:
        return "admin"
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT role FROM roles WHERE user_id = ? LIMIT 1", (user_id,))
        row = await cursor.fetchone()
        if row and row["role"]:
            return row["role"]
    return "user"


async def _sqlite_set_user_role(user_id: int, role: str) -> None:
    """Assigns a role to a user in SQLite ('admin', 'moderator', 'user')."""
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO roles (user_id, role)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET role = excluded.role
            """,
            (user_id, role.lower().strip())
        )
        await conn.commit()


async def _sqlite_toggle_favorite(user_id: int, book_id: int) -> bool:
    """Toggles bookmark for a book in SQLite. Returns True if added, False if removed."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND book_id = ? LIMIT 1",
            (user_id, book_id)
        )
        row = await cursor.fetchone()
        if row:
            await conn.execute("DELETE FROM favorites WHERE user_id = ? AND book_id = ?", (user_id, book_id))
            await conn.commit()
            return False
        else:
            await conn.execute(
                "INSERT INTO favorites (user_id, book_id) VALUES (?, ?)",
                (user_id, book_id)
            )
            await conn.commit()
            return True


async def _sqlite_is_favorite(user_id: int, book_id: int) -> bool:
    """Checks if a book is bookmarked by a user in SQLite."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND book_id = ? LIMIT 1",
            (user_id, book_id)
        )
        return (await cursor.fetchone()) is not None


async def _sqlite_get_user_favorites(user_id: int) -> List[Dict[str, Any]]:
    """Retrieves all bookmarked books for a user from SQLite."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT b.id, b.clean_title, b.clean_author, b.cover_url, b.description,
                   b.rating, b.genres, b.year,
                   GROUP_CONCAT(DISTINCT f.format) as formats,
                   fav.added_at
            FROM favorites fav
            JOIN books b ON b.id = fav.book_id
            LEFT JOIN book_files f ON f.book_id = b.id
            WHERE fav.user_id = ?
            GROUP BY b.id, fav.added_at
            ORDER BY fav.added_at DESC
            """,
            (user_id,)
        )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            formats_str = r["formats"] or ""
            formats_list = [fmt.strip() for fmt in formats_str.split(",") if fmt.strip()]
            results.append({
                "id": r["id"],
                "clean_title": r["clean_title"],
                "clean_author": r["clean_author"] or "Unknown Author",
                "cover_url": r["cover_url"],
                "description": r["description"],
                "rating": r["rating"],
                "genres": r["genres"],
                "year": r["year"],
                "formats": formats_list,
                "added_at": r["added_at"]
            })
        return results


async def _sqlite_create_request(user_id: int, query: str) -> int:
    """Inserts a community book request in SQLite. Returns request ID."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO requests (user_id, query) VALUES (?, ?)",
            (user_id, query.strip())
        )
        await conn.commit()
        return cursor.lastrowid


async def _sqlite_update_request_status(request_id: int, status: str) -> Optional[Dict[str, Any]]:
    """Updates status of a book request in SQLite ('fulfilled', 'rejected', etc.). Returns request info."""
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT id, user_id, query, status, created_at FROM requests WHERE id = ? LIMIT 1", (request_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        await conn.execute("UPDATE requests SET status = ? WHERE id = ?", (status.lower().strip(), request_id))
        await conn.commit()
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "query": row["query"],
            "status": status.lower().strip(),
            "created_at": row["created_at"]
        }


async def _sqlite_get_requests(status: Optional[str] = "pending", limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieves book requests from SQLite."""
    async with get_connection() as conn:
        if status:
            cursor = await conn.execute(
                "SELECT id, user_id, query, status, created_at FROM requests WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status.lower().strip(), limit)
            )
        else:
            cursor = await conn.execute(
                "SELECT id, user_id, query, status, created_at FROM requests ORDER BY id DESC LIMIT ?",
                (limit,)
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "query": r["query"],
                "status": r["status"],
                "created_at": r["created_at"]
            }
            for r in rows
        ]


async def _sqlite_delete_book_file(file_id: int) -> bool:
    """Deletes a specific book_files row from SQLite. Returns True if deleted."""
    async with get_connection() as conn:
        cursor = await conn.execute("DELETE FROM book_files WHERE id = ?", (file_id,))
        await conn.commit()
        return cursor.rowcount > 0


# ==============================================================================
# POSTGRESQL IMPLEMENTATION (MODE A)
# ==============================================================================

async def _pg_init_db() -> None:
    """Initializes PostgreSQL tables, pg_trgm extension, and tsvector indices."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        has_trgm = False
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
            has_trgm = True
        except Exception as e:
            logger.warning(f"Could not enable pg_trgm extension (continuing with tsvector): {e}")

        # Books table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id SERIAL PRIMARY KEY,
                clean_title TEXT NOT NULL,
                clean_author TEXT DEFAULT 'Unknown Author',
                canonical_title TEXT,
                canonical_author TEXT,
                cover_url TEXT,
                description TEXT,
                rating REAL,
                genres TEXT,
                year TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(clean_title, clean_author)
            );
        """)

        # Book Files table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS book_files (
                id SERIAL PRIMARY KEY,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                format TEXT NOT NULL,
                file_id TEXT NOT NULL UNIQUE,
                file_unique_id TEXT,
                channel_message_id INTEGER,
                file_size BIGINT,
                file_name TEXT,
                duration INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Migration columns
        try:
            await conn.execute("ALTER TABLE book_files ADD COLUMN IF NOT EXISTS channel_message_id INTEGER;")
            await conn.execute("ALTER TABLE book_files ADD COLUMN IF NOT EXISTS file_unique_id TEXT;")
        except Exception as e:
            logger.debug(f"PostgreSQL column migration note: {e}")

        # Users table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_downloads INTEGER DEFAULT 0
            );
        """)

        # RBAC Roles table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS roles (
                user_id BIGINT PRIMARY KEY,
                role TEXT DEFAULT 'user'
            );
        """)

        # User Favorites / Bookmarks table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id BIGINT NOT NULL,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, book_id)
            );
        """)

        # Community Requests table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                query TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Indices
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_books_canonical ON books(canonical_title, canonical_author);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_book_id ON book_files(book_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_format ON book_files(book_id, format);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_msg_id ON book_files(channel_message_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_unique_id ON book_files(file_unique_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_favorites_user_id ON favorites(user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);")

        # Full-Text Search tsvector index
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_books_fts ON books USING gin(
                to_tsvector('simple', clean_title || ' ' || COALESCE(clean_author, ''))
            );
        """)

        if has_trgm:
            try:
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_books_trgm_title ON books USING gin(clean_title gin_trgm_ops);")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_books_trgm_author ON books USING gin(clean_author gin_trgm_ops);")
            except Exception as e:
                logger.warning(f"Could not create pg_trgm GIN indices: {e}")

        logger.info("PostgreSQL database schema initialized successfully.")


async def _pg_upsert_book_and_file(
    clean_title: str,
    clean_author: str,
    canonical_title: str,
    canonical_author: str,
    format_type: str,
    file_id: str,
    file_size: Optional[int] = None,
    file_name: Optional[str] = None,
    duration: Optional[int] = None,
    channel_message_id: Optional[int] = None,
    file_unique_id: Optional[str] = None,
    cover_url: Optional[str] = None,
    description: Optional[str] = None
) -> Tuple[int, bool, bool]:
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM books
            WHERE (canonical_title = $1 AND canonical_author = $2)
               OR (clean_title = $3 AND clean_author = $4)
            LIMIT 1
            """,
            canonical_title, canonical_author, clean_title, clean_author
        )

        is_new_book = False
        if row:
            book_id = row["id"]
            if cover_url or description:
                await conn.execute(
                    """
                    UPDATE books
                    SET cover_url = COALESCE(cover_url, $1),
                        description = COALESCE(description, $2)
                    WHERE id = $3
                    """,
                    cover_url, description, book_id
                )
        else:
            insert_row = await conn.fetchrow(
                """
                INSERT INTO books (clean_title, clean_author, canonical_title, canonical_author, cover_url, description)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (clean_title, clean_author) DO NOTHING
                RETURNING id
                """,
                clean_title, clean_author, canonical_title, canonical_author, cover_url, description
            )
            if insert_row:
                book_id = insert_row["id"]
                is_new_book = True
            else:
                existing = await conn.fetchrow(
                    "SELECT id FROM books WHERE clean_title = $1 AND clean_author = $2",
                    clean_title, clean_author
                )
                book_id = existing["id"]

        # Check if file already exists
        existing_file = None
        if file_id:
            existing_file = await conn.fetchrow("SELECT id FROM book_files WHERE file_id = $1", file_id)
        if not existing_file and file_unique_id:
            existing_file = await conn.fetchrow("SELECT id FROM book_files WHERE file_unique_id = $1", file_unique_id)
        if not existing_file and channel_message_id:
            existing_file = await conn.fetchrow("SELECT id FROM book_files WHERE channel_message_id = $1", channel_message_id)

        is_new_file = False
        if existing_file:
            if channel_message_id or file_unique_id:
                await conn.execute(
                    """
                    UPDATE book_files
                    SET channel_message_id = COALESCE(channel_message_id, $1),
                        file_unique_id = COALESCE(file_unique_id, $2)
                    WHERE id = $3
                    """,
                    channel_message_id, file_unique_id, existing_file["id"]
                )
        else:
            file_row = await conn.fetchrow(
                """
                INSERT INTO book_files (book_id, format, file_id, file_unique_id, channel_message_id, file_size, file_name, duration)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (file_id) DO NOTHING
                RETURNING id
                """,
                book_id, format_type, file_id, file_unique_id, channel_message_id, file_size, file_name, duration
            )
            is_new_file = file_row is not None

        return book_id, is_new_book, is_new_file


async def _pg_update_book_metadata(
    book_id: int,
    cover_url: Optional[str] = None,
    description: Optional[str] = None,
    rating: Optional[float] = None,
    genres: Optional[str] = None,
    year: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None
) -> None:
    pool = await get_pg_pool()
    canonical_title = title.lower().strip() if title else None
    canonical_author = author.lower().strip() if author else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE books
            SET cover_url = COALESCE($1, cover_url),
                description = COALESCE($2, description),
                rating = COALESCE($3, rating),
                genres = COALESCE($4, genres),
                year = COALESCE($5, year),
                clean_title = COALESCE($6, clean_title),
                clean_author = COALESCE($7, clean_author),
                canonical_title = COALESCE($8, canonical_title),
                canonical_author = COALESCE($9, canonical_author)
            WHERE id = $10
            """,
            cover_url, description, rating, genres, year, title, author, canonical_title, canonical_author, book_id
        )


async def _pg_search_books(query: str, limit: int = 5, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    tokens = re.findall(r"\w+", query)
    if not tokens:
        return [], 0

    clean_tokens = [re.sub(r"[^\w]", "", t) for t in tokens if re.sub(r"[^\w]", "", t)]
    tsquery_str = " & ".join(f"{t}:*" for t in clean_tokens) if clean_tokens else query.strip()
    like_pattern = f"%{query.strip()}%"

    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        try:
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total
                FROM books
                WHERE (
                    to_tsvector('simple', clean_title || ' ' || COALESCE(clean_author, '')) @@ to_tsquery('simple', $1)
                    OR clean_title ILIKE $2
                    OR clean_author ILIKE $2
                )
                """,
                tsquery_str, like_pattern
            )
            total = count_row["total"] if count_row else 0
            if total == 0:
                return [], 0

            rows = await conn.fetch(
                """
                SELECT b.id, b.clean_title, b.clean_author, b.cover_url, b.description,
                       b.rating, b.genres, b.year,
                       STRING_AGG(DISTINCT f.format, ',') as formats,
                       ts_rank(to_tsvector('simple', b.clean_title || ' ' || COALESCE(b.clean_author, '')), to_tsquery('simple', $1)) AS rank
                FROM books b
                LEFT JOIN book_files f ON f.book_id = b.id
                WHERE (
                    to_tsvector('simple', b.clean_title || ' ' || COALESCE(b.clean_author, '')) @@ to_tsquery('simple', $1)
                    OR b.clean_title ILIKE $2
                    OR b.clean_author ILIKE $2
                )
                GROUP BY b.id
                ORDER BY rank DESC, b.id DESC
                LIMIT $3 OFFSET $4
                """,
                tsquery_str, like_pattern, limit, offset
            )
        except Exception as e:
            logger.warning(f"PostgreSQL tsquery search encountered issue ({e}), falling back to ILIKE.")
            count_row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total
                FROM books
                WHERE clean_title ILIKE $1 OR clean_author ILIKE $1
                """,
                like_pattern
            )
            total = count_row["total"] if count_row else 0
            if total == 0:
                return [], 0

            rows = await conn.fetch(
                """
                SELECT b.id, b.clean_title, b.clean_author, b.cover_url, b.description,
                       b.rating, b.genres, b.year,
                       STRING_AGG(DISTINCT f.format, ',') as formats
                FROM books b
                LEFT JOIN book_files f ON f.book_id = b.id
                WHERE b.clean_title ILIKE $1 OR b.clean_author ILIKE $1
                GROUP BY b.id
                ORDER BY b.id DESC
                LIMIT $2 OFFSET $3
                """,
                like_pattern, limit, offset
            )

        results = []
        for r in rows:
            formats_str = r["formats"] or ""
            formats_list = [fmt.strip() for fmt in formats_str.split(",") if fmt.strip()]
            results.append({
                "id": r["id"],
                "clean_title": r["clean_title"],
                "clean_author": r["clean_author"],
                "cover_url": r["cover_url"],
                "description": r["description"],
                "rating": r["rating"],
                "genres": r["genres"],
                "year": r["year"],
                "formats": formats_list,
            })
        return results, total


async def _pg_fuzzy_search_books(query: str, limit: int = 5, score_cutoff: float = 65.0) -> Tuple[List[Dict[str, Any]], int]:
    try:
        from rapidfuzz import process, fuzz
    except ImportError:
        return [], 0

    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT b.id, b.clean_title, b.clean_author, b.cover_url, b.description,
                   b.rating, b.genres, b.year,
                   STRING_AGG(DISTINCT f.format, ',') as formats
            FROM books b
            LEFT JOIN book_files f ON f.book_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT 500
            """
        )
        if not rows:
            return [], 0

        candidates = []
        clean_titles = []
        for r in rows:
            formats_str = r["formats"] or ""
            formats_list = [fmt.strip() for fmt in formats_str.split(",") if fmt.strip()]
            candidates.append({
                "id": r["id"],
                "clean_title": r["clean_title"],
                "clean_author": r["clean_author"],
                "cover_url": r["cover_url"],
                "description": r["description"],
                "rating": r["rating"],
                "genres": r["genres"],
                "year": r["year"],
                "formats": formats_list,
            })
            clean_titles.append(r["clean_title"])

        matches = await asyncio.to_thread(
            process.extract,
            query,
            clean_titles,
            scorer=fuzz.token_set_ratio,
            score_cutoff=score_cutoff,
            limit=limit
        )

        if not matches:
            full_strings = [f"{c['clean_title']} {c['clean_author']}".strip() for c in candidates]
            matches = await asyncio.to_thread(
                process.extract,
                query,
                full_strings,
                scorer=fuzz.token_set_ratio,
                score_cutoff=score_cutoff,
                limit=limit
            )

        matched_books = [candidates[idx] for _, score, idx in matches]
        return matched_books, len(matched_books)


async def _pg_get_book_by_id(book_id: int) -> Optional[Dict[str, Any]]:
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        book_row = await conn.fetchrow("SELECT * FROM books WHERE id = $1", book_id)
        if not book_row:
            return None

        files = await conn.fetch(
            """
            SELECT id, format, file_id, file_unique_id, channel_message_id, file_size, file_name, duration
            FROM book_files
            WHERE book_id = $1
            ORDER BY format ASC
            """,
            book_id
        )

        file_list = [
            {
                "id": f["id"],
                "format": f["format"],
                "file_id": f["file_id"],
                "file_unique_id": f["file_unique_id"],
                "channel_message_id": f["channel_message_id"],
                "file_size": f["file_size"],
                "file_name": f["file_name"],
                "duration": f["duration"]
            }
            for f in files
        ]

        return {
            "id": book_row["id"],
            "clean_title": book_row["clean_title"],
            "clean_author": book_row["clean_author"],
            "cover_url": book_row["cover_url"],
            "description": book_row["description"],
            "rating": book_row["rating"],
            "genres": book_row["genres"],
            "year": book_row["year"],
            "files": file_list
        }


async def _pg_get_file_by_id(file_id: int) -> Optional[Dict[str, Any]]:
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT f.*, b.clean_title, b.clean_author
            FROM book_files f
            JOIN books b ON b.id = f.book_id
            WHERE f.id = $1
            """,
            file_id
        )
        if not row:
            return None
        return dict(row)


async def _pg_register_or_update_user(user_id: int, username: Optional[str] = None) -> None:
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, users.username)
            """,
            user_id, username
        )


async def _pg_increment_user_downloads(user_id: int) -> None:
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, total_downloads)
            VALUES ($1, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                total_downloads = users.total_downloads + 1
            """,
            user_id
        )


async def _pg_get_stats() -> Dict[str, Any]:
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        total_books = await conn.fetchval("SELECT COUNT(*) FROM books") or 0
        f_rows = await conn.fetch(
            """
            SELECT format, COUNT(*) AS count
            FROM book_files
            GROUP BY format
            """
        )
        format_counts = {r["format"]: r["count"] for r in f_rows}
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
        total_downloads = await conn.fetchval("SELECT SUM(total_downloads) FROM users") or 0

        return {
            "total_books": total_books,
            "format_counts": format_counts,
            "total_users": total_users,
            "total_downloads": total_downloads,
        }


async def _pg_get_all_catalog_books() -> List[Dict[str, Any]]:
    """Loads all books with formats for in-memory cache preloading."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT b.id, b.clean_title, b.clean_author, b.cover_url, b.description,
                   b.rating, b.genres, b.year,
                   STRING_AGG(DISTINCT f.format, ',') as formats
            FROM books b
            LEFT JOIN book_files f ON f.book_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
            """
        )
        results = []
        for r in rows:
            formats_str = r["formats"] or ""
            formats_list = [fmt.strip() for fmt in formats_str.split(",") if fmt.strip()]
            results.append({
                "id": r["id"],
                "clean_title": r["clean_title"],
                "clean_author": r["clean_author"] or "Unknown Author",
                "cover_url": r["cover_url"],
                "description": r["description"],
                "rating": r["rating"],
                "genres": r["genres"],
                "year": r["year"],
                "formats": formats_list,
            })
        return results


async def _pg_get_user_role(user_id: int) -> str:
    """Returns the role of a user from PostgreSQL ('admin', 'moderator', 'user')."""
    if user_id in settings.ADMIN_IDS:
        return "admin"
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT role FROM roles WHERE user_id = $1 LIMIT 1", user_id)
        if row and row["role"]:
            return row["role"]
    return "user"


async def _pg_set_user_role(user_id: int, role: str) -> None:
    """Assigns a role to a user in PostgreSQL ('admin', 'moderator', 'user')."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO roles (user_id, role)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role
            """,
            user_id, role.lower().strip()
        )


async def _pg_toggle_favorite(user_id: int, book_id: int) -> bool:
    """Toggles bookmark for a book in PostgreSQL. Returns True if added, False if removed."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM favorites WHERE user_id = $1 AND book_id = $2 LIMIT 1",
            user_id, book_id
        )
        if row:
            await conn.execute("DELETE FROM favorites WHERE user_id = $1 AND book_id = $2", user_id, book_id)
            return False
        else:
            await conn.execute(
                "INSERT INTO favorites (user_id, book_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, book_id
            )
            return True


async def _pg_is_favorite(user_id: int, book_id: int) -> bool:
    """Checks if a book is bookmarked by a user in PostgreSQL."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM favorites WHERE user_id = $1 AND book_id = $2 LIMIT 1",
            user_id, book_id
        )
        return row is not None


async def _pg_get_user_favorites(user_id: int) -> List[Dict[str, Any]]:
    """Retrieves all bookmarked books for a user from PostgreSQL."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT b.id, b.clean_title, b.clean_author, b.cover_url, b.description,
                   b.rating, b.genres, b.year,
                   STRING_AGG(DISTINCT f.format, ',') as formats,
                   fav.added_at
            FROM favorites fav
            JOIN books b ON b.id = fav.book_id
            LEFT JOIN book_files f ON f.book_id = b.id
            WHERE fav.user_id = $1
            GROUP BY b.id, fav.added_at
            ORDER BY fav.added_at DESC
            """,
            user_id
        )
        results = []
        for r in rows:
            formats_str = r["formats"] or ""
            formats_list = [fmt.strip() for fmt in formats_str.split(",") if fmt.strip()]
            results.append({
                "id": r["id"],
                "clean_title": r["clean_title"],
                "clean_author": r["clean_author"] or "Unknown Author",
                "cover_url": r["cover_url"],
                "description": r["description"],
                "rating": r["rating"],
                "genres": r["genres"],
                "year": r["year"],
                "formats": formats_list,
                "added_at": r["added_at"]
            })
        return results


async def _pg_create_request(user_id: int, query: str) -> int:
    """Inserts a community book request in PostgreSQL. Returns request ID."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO requests (user_id, query) VALUES ($1, $2) RETURNING id",
            user_id, query.strip()
        )
        return row["id"]


async def _pg_update_request_status(request_id: int, status: str) -> Optional[Dict[str, Any]]:
    """Updates status of a book request in PostgreSQL ('fulfilled', 'rejected', etc.). Returns request info."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE requests
            SET status = $1
            WHERE id = $2
            RETURNING id, user_id, query, status, created_at
            """,
            status.lower().strip(), request_id
        )
        if not row:
            return None
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "query": row["query"],
            "status": row["status"],
            "created_at": row["created_at"]
        }


async def _pg_get_requests(status: Optional[str] = "pending", limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieves book requests from PostgreSQL."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT id, user_id, query, status, created_at FROM requests WHERE status = $1 ORDER BY id DESC LIMIT $2",
                status.lower().strip(), limit
            )
        else:
            rows = await conn.fetch(
                "SELECT id, user_id, query, status, created_at FROM requests ORDER BY id DESC LIMIT $1",
                limit
            )
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "query": r["query"],
                "status": r["status"],
                "created_at": r["created_at"]
            }
            for r in rows
        ]


async def _pg_delete_book_file(file_id: int) -> bool:
    """Deletes a specific book_files row from PostgreSQL. Returns True if deleted."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        res = await conn.execute("DELETE FROM book_files WHERE id = $1", file_id)
        # res looks like 'DELETE 1'
        return res != "DELETE 0"


# ==============================================================================
# UNIFIED PUBLIC API (AUTOMATIC ENGINE DISPATCH)
# ==============================================================================

async def init_db() -> None:
    """Initializes tables, virtual tables, triggers, and indices on active database."""
    if is_postgres():
        await _pg_init_db()
    else:
        await _sqlite_init_db()


async def is_file_indexed(
    channel_message_id: Optional[int] = None,
    file_unique_id: Optional[str] = None,
    file_id: Optional[str] = None
) -> bool:
    """Checks if a file has already been ingested via message ID, unique ID, or file ID."""
    if not channel_message_id and not file_unique_id and not file_id:
        return False

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            if channel_message_id:
                row = await conn.fetchrow("SELECT id FROM book_files WHERE channel_message_id = $1 LIMIT 1", channel_message_id)
                if row:
                    return True
            if file_unique_id:
                row = await conn.fetchrow("SELECT id FROM book_files WHERE file_unique_id = $1 LIMIT 1", file_unique_id)
                if row:
                    return True
            if file_id:
                row = await conn.fetchrow("SELECT id FROM book_files WHERE file_id = $1 LIMIT 1", file_id)
                if row:
                    return True
            return False
    else:
        async with get_connection() as conn:
            if channel_message_id:
                cur = await conn.execute("SELECT id FROM book_files WHERE channel_message_id = ? LIMIT 1", (channel_message_id,))
                if await cur.fetchone():
                    return True
            if file_unique_id:
                cur = await conn.execute("SELECT id FROM book_files WHERE file_unique_id = ? LIMIT 1", (file_unique_id,))
                if await cur.fetchone():
                    return True
            if file_id:
                cur = await conn.execute("SELECT id FROM book_files WHERE file_id = ? LIMIT 1", (file_id,))
                if await cur.fetchone():
                    return True
            return False


async def upsert_book_and_file(
    clean_title: str,
    clean_author: str,
    canonical_title: str,
    canonical_author: str,
    format_type: str,
    file_id: str,
    file_size: Optional[int] = None,
    file_name: Optional[str] = None,
    duration: Optional[int] = None,
    channel_message_id: Optional[int] = None,
    file_unique_id: Optional[str] = None,
    cover_url: Optional[str] = None,
    description: Optional[str] = None,
    **kwargs: Any
) -> Tuple[int, bool, bool]:
    """Inserts or matches a book and attaches media format. Returns: (book_id, is_new_book, is_new_file)."""
    if is_postgres():
        return await _pg_upsert_book_and_file(
            clean_title, clean_author, canonical_title, canonical_author,
            format_type, file_id, file_size, file_name, duration,
            channel_message_id, file_unique_id,
            cover_url=cover_url, description=description
        )
    else:
        return await _sqlite_upsert_book_and_file(
            clean_title, clean_author, canonical_title, canonical_author,
            format_type, file_id, file_size, file_name, duration,
            channel_message_id, file_unique_id,
            cover_url=cover_url, description=description
        )


async def update_book_metadata(
    book_id: int,
    cover_url: Optional[str] = None,
    description: Optional[str] = None,
    rating: Optional[float] = None,
    genres: Optional[str] = None,
    year: Optional[str] = None,
    title: Optional[str] = None,
    author: Optional[str] = None
) -> None:
    """Updates enriched metadata, title, or author for an existing book."""
    if is_postgres():
        await _pg_update_book_metadata(book_id, cover_url, description, rating, genres, year, title=title, author=author)
    else:
        await _sqlite_update_book_metadata(book_id, cover_url, description, rating, genres, year, title=title, author=author)


async def get_user_role(user_id: int) -> str:
    """Returns the role of a user ('admin', 'moderator', 'user')."""
    if is_postgres():
        return await _pg_get_user_role(user_id)
    else:
        return await _sqlite_get_user_role(user_id)


async def set_user_role(user_id: int, role: str) -> None:
    """Sets the role of a user ('admin', 'moderator', 'user')."""
    if is_postgres():
        await _pg_set_user_role(user_id, role)
    else:
        await _sqlite_set_user_role(user_id, role)


async def toggle_favorite(user_id: int, book_id: int) -> bool:
    """Toggles bookmark status of a book. Returns True if added, False if removed."""
    if is_postgres():
        return await _pg_toggle_favorite(user_id, book_id)
    else:
        return await _sqlite_toggle_favorite(user_id, book_id)


async def is_favorite(user_id: int, book_id: int) -> bool:
    """Checks if a book is currently bookmarked by user."""
    if is_postgres():
        return await _pg_is_favorite(user_id, book_id)
    else:
        return await _sqlite_is_favorite(user_id, book_id)


async def get_user_favorites(user_id: int) -> List[Dict[str, Any]]:
    """Retrieves all favorite books for a user."""
    if is_postgres():
        return await _pg_get_user_favorites(user_id)
    else:
        return await _sqlite_get_user_favorites(user_id)


async def create_request(user_id: int, query: str) -> int:
    """Creates a new book request. Returns request ID."""
    if is_postgres():
        return await _pg_create_request(user_id, query)
    else:
        return await _sqlite_create_request(user_id, query)


async def update_request_status(request_id: int, status: str) -> Optional[Dict[str, Any]]:
    """Updates status of a request ('fulfilled', 'rejected', etc.). Returns request info."""
    if is_postgres():
        return await _pg_update_request_status(request_id, status)
    else:
        return await _sqlite_update_request_status(request_id, status)


async def get_requests(status: Optional[str] = "pending", limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieves book requests."""
    if is_postgres():
        return await _pg_get_requests(status=status, limit=limit)
    else:
        return await _sqlite_get_requests(status=status, limit=limit)


async def delete_book_file(file_id: int) -> bool:
    """Deletes a specific book_files row from database. Returns True if deleted."""
    if is_postgres():
        return await _pg_delete_book_file(file_id)
    else:
        return await _sqlite_delete_book_file(file_id)


async def get_all_catalog_books() -> List[Dict[str, Any]]:
    """Retrieves all book catalog items for in-memory preloading."""
    if is_postgres():
        return await _pg_get_all_catalog_books()
    else:
        return await _sqlite_get_all_catalog_books()


async def search_books(query: str, limit: int = 5, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """
    Searches books using in-memory catalog cache if preloaded (<2ms latency).
    Falls back to PostgreSQL / SQLite full-text search if cache is not loaded.
    """
    try:
        from services.search import catalog_cache
        if catalog_cache.is_loaded:
            return catalog_cache.search(query, limit=limit, offset=offset)
    except Exception as e:
        logger.warning(f"In-memory catalog cache lookup failed ({e}), falling back to database.")

    if is_postgres():
        results, total = await _pg_search_books(query, limit, offset)
        if total == 0 and offset == 0:
            logger.info(f"PostgreSQL search returned 0 results for '{query}'. Running fuzzy search fallback...")
            results, total = await _pg_fuzzy_search_books(query, limit=limit, score_cutoff=65.0)
        return results, total
    else:
        results, total = await _sqlite_search_books(query, limit, offset)
        if total == 0 and offset == 0:
            logger.info(f"SQLite FTS5 returned 0 results for '{query}'. Running fuzzy search fallback...")
            results, total = await _sqlite_fuzzy_search_books(query, limit=limit, score_cutoff=65.0)
        return results, total


async def get_book_by_id(book_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves full details of a book and its available files."""
    if is_postgres():
        return await _pg_get_book_by_id(book_id)
    else:
        return await _sqlite_get_book_by_id(book_id)


async def get_file_by_id(file_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves a specific file record for delivery."""
    if is_postgres():
        return await _pg_get_file_by_id(file_id)
    else:
        return await _sqlite_get_file_by_id(file_id)


async def register_or_update_user(user_id: int, username: Optional[str] = None) -> None:
    """Inserts a user if new or updates their username."""
    if is_postgres():
        await _pg_register_or_update_user(user_id, username)
    else:
        await _sqlite_register_or_update_user(user_id, username)


async def increment_user_downloads(user_id: int) -> None:
    """Increments the total download counter for a user."""
    if is_postgres():
        await _pg_increment_user_downloads(user_id)
    else:
        await _sqlite_increment_user_downloads(user_id)


async def get_all_user_ids() -> List[int]:
    """Retrieves all registered user IDs for broadcasting."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users")
            return [int(r["user_id"]) for r in rows]
    else:
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT user_id FROM users")
            rows = await cursor.fetchall()
            return [int(r["user_id"]) for r in rows]


async def get_stats() -> Dict[str, Any]:
    """Computes administrative metrics."""
    if is_postgres():
        return await _pg_get_stats()
    else:
        return await _sqlite_get_stats()


async def get_indexed_message_ids() -> set:
    """Returns a set of all channel_message_ids already indexed in the database."""
    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT channel_message_id FROM book_files WHERE channel_message_id IS NOT NULL")
            return {r["channel_message_id"] for r in rows}
    else:
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT channel_message_id FROM book_files WHERE channel_message_id IS NOT NULL")
            rows = await cursor.fetchall()
            return {r["channel_message_id"] for r in rows}


async def batch_upsert_books_and_files(items: List[Dict[str, Any]]) -> Tuple[int, int]:
    """
    Inserts a batch of books and files within a single database transaction using bulk operations.
    Returns: (new_books_count, new_files_count)
    """
    if not items:
        return 0, 0

    if is_postgres():
        new_books_count = 0
        new_files_count = 0
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. Deduplicate books in memory within the batch
                distinct_books: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
                for item in items:
                    key = (item["clean_title"], item.get("clean_author") or "Unknown Author")
                    if key not in distinct_books:
                        distinct_books[key] = (
                            item["clean_title"],
                            item.get("clean_author") or "Unknown Author",
                            item.get("canonical_title"),
                            item.get("canonical_author"),
                            item.get("cover_url"),
                            item.get("description")
                        )

                # 2. Bulk insert distinct books using executemany in 1 round-trip
                book_insert_records = list(distinct_books.values())
                await conn.executemany(
                    """
                    INSERT INTO books (clean_title, clean_author, canonical_title, canonical_author, cover_url, description)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (clean_title, clean_author) DO NOTHING
                    """,
                    book_insert_records
                )

                # 3. Fetch book IDs for distinct books in 1 round-trip
                clean_titles = [k[0] for k in distinct_books.keys()]
                rows = await conn.fetch(
                    "SELECT id, clean_title, clean_author FROM books WHERE clean_title = ANY($1::text[])",
                    clean_titles
                )
                book_map = {(r["clean_title"], r["clean_author"]): r["id"] for r in rows}

                # 4. Prepare file records
                file_records = []
                for item in items:
                    key = (item["clean_title"], item.get("clean_author") or "Unknown Author")
                    book_id = book_map.get(key)
                    if not book_id:
                        b_row = await conn.fetchrow(
                            "SELECT id FROM books WHERE clean_title = $1 AND clean_author = $2 LIMIT 1",
                            key[0], key[1]
                        )
                        book_id = b_row["id"] if b_row else None
                    if book_id:
                        file_records.append((
                            book_id,
                            item["format_type"],
                            item["file_id"],
                            item.get("file_unique_id"),
                            item.get("channel_message_id"),
                            item.get("file_size"),
                            item.get("file_name"),
                            item.get("duration")
                        ))

                # 5. Bulk upsert file records using executemany in 1 round-trip
                if file_records:
                    await conn.executemany(
                        """
                        INSERT INTO book_files (book_id, format, file_id, file_unique_id, channel_message_id, file_size, file_name, duration)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (file_id) DO UPDATE SET
                            channel_message_id = COALESCE(book_files.channel_message_id, EXCLUDED.channel_message_id),
                            file_unique_id = COALESCE(book_files.file_unique_id, EXCLUDED.file_unique_id)
                        """,
                        file_records
                    )
                    new_files_count = len(file_records)
        return new_books_count, new_files_count
    else:
        new_books_count = 0
        new_files_count = 0
        async with get_connection() as conn:
            distinct_books_sqlite: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
            for item in items:
                key = (item["clean_title"], item.get("clean_author") or "Unknown Author")
                if key not in distinct_books_sqlite:
                    distinct_books_sqlite[key] = (
                        item["clean_title"],
                        item.get("clean_author") or "Unknown Author",
                        item.get("canonical_title"),
                        item.get("canonical_author"),
                        item.get("cover_url"),
                        item.get("description")
                    )

            await conn.executemany(
                """
                INSERT OR IGNORE INTO books (clean_title, clean_author, canonical_title, canonical_author, cover_url, description)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                list(distinct_books_sqlite.values())
            )

            book_map_sqlite = {}
            for title, author in distinct_books_sqlite.keys():
                cur = await conn.execute(
                    "SELECT id FROM books WHERE clean_title = ? AND clean_author = ? LIMIT 1",
                    (title, author)
                )
                r = await cur.fetchone()
                if r:
                    book_map_sqlite[(title, author)] = r["id"]

            file_records_sqlite = []
            for item in items:
                key = (item["clean_title"], item.get("clean_author") or "Unknown Author")
                book_id = book_map_sqlite.get(key)
                if book_id:
                    file_records_sqlite.append((
                        book_id,
                        item["format_type"],
                        item["file_id"],
                        item.get("file_unique_id"),
                        item.get("channel_message_id"),
                        item.get("file_size"),
                        item.get("file_name"),
                        item.get("duration")
                    ))

            if file_records_sqlite:
                await conn.executemany(
                    """
                    INSERT INTO book_files (book_id, format, file_id, file_unique_id, channel_message_id, file_size, file_name, duration)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        channel_message_id = COALESCE(book_files.channel_message_id, excluded.channel_message_id),
                        file_unique_id = COALESCE(book_files.file_unique_id, excluded.file_unique_id)
                    """,
                    file_records_sqlite
                )
                new_files_count = len(file_records_sqlite)

            await conn.commit()
        return new_books_count, new_files_count


# Alias for compatibility with backfill and external modules
insert_or_update_book = upsert_book_and_file

