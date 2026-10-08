import re
import aiosqlite
import logging
from typing import Dict, List, Optional, Tuple, Any
from database.connection import get_connection

logger = logging.getLogger(__name__)


async def init_db() -> None:
    """Initializes tables, virtual tables, triggers, and indices."""
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
                format TEXT NOT NULL,          -- 'AUDIO', 'EPUB', 'PDF', 'ZIP'
                file_id TEXT NOT NULL UNIQUE,   -- Telegram file_id for direct delivery
                file_size INTEGER,              -- in bytes
                file_name TEXT,
                duration INTEGER,               -- in seconds for audiobooks
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
            );
        """)

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

        # Indices for optimal lookup
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_books_canonical ON books(canonical_title, canonical_author);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_book_id ON book_files(book_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_book_files_format ON book_files(book_id, format);")

        await conn.commit()
        logger.info("Database schema initialized successfully.")


def _sanitize_fts_query(query: str) -> str:
    """Prepares user query for safe FTS5 prefix search."""
    tokens = re.findall(r"\w+", query)
    if not tokens:
        return '""'
    # Format each token as prefix match
    return " ".join(f'"{t}"*' for t in tokens)


async def upsert_book_and_file(
    clean_title: str,
    clean_author: str,
    canonical_title: str,
    canonical_author: str,
    format_type: str,
    file_id: str,
    file_size: Optional[int] = None,
    file_name: Optional[str] = None,
    duration: Optional[int] = None
) -> Tuple[int, bool, bool]:
    """
    Inserts or matches a book and attaches the media format.
    Returns: (book_id, is_new_book, is_new_file)
    """
    async with get_connection() as conn:
        # Check if book already exists by canonical representation or clean title/author
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
        else:
            try:
                cursor = await conn.execute(
                    """
                    INSERT INTO books (clean_title, clean_author, canonical_title, canonical_author)
                    VALUES (?, ?, ?, ?)
                    """,
                    (clean_title, clean_author, canonical_title, canonical_author)
                )
                book_id = cursor.lastrowid
                is_new_book = True
            except aiosqlite.IntegrityError:
                # Concurrency edge case fallback
                cursor = await conn.execute(
                    "SELECT id FROM books WHERE clean_title = ? AND clean_author = ?",
                    (clean_title, clean_author)
                )
                existing = await cursor.fetchone()
                book_id = existing["id"]

        # Insert or ignore file
        is_new_file = False
        try:
            cursor = await conn.execute(
                """
                INSERT INTO book_files (book_id, format, file_id, file_size, file_name, duration)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (book_id, format_type, file_id, file_size, file_name, duration)
            )
            is_new_file = True
        except aiosqlite.IntegrityError:
            # File already exists
            pass

        await conn.commit()
        return book_id, is_new_book, is_new_file


async def update_book_metadata(
    book_id: int,
    cover_url: Optional[str] = None,
    description: Optional[str] = None,
    rating: Optional[float] = None,
    genres: Optional[str] = None,
    year: Optional[str] = None
) -> None:
    """Updates enriched metadata for an existing book."""
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE books
            SET cover_url = COALESCE(?, cover_url),
                description = COALESCE(?, description),
                rating = COALESCE(?, rating),
                genres = COALESCE(?, genres),
                year = COALESCE(?, year)
            WHERE id = ?
            """,
            (cover_url, description, rating, genres, year, book_id)
        )
        await conn.commit()


async def search_books(query: str, limit: int = 5, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """
    Searches books using FTS5 index.
    Returns (results_list, total_count).
    Each result contains book details and available formats.
    """
    clean_q = _sanitize_fts_query(query)
    if clean_q == '""':
        return [], 0

    async with get_connection() as conn:
        # Get total matching count
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

        # Query paginated books with format aggregation
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


async def get_book_by_id(book_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves full details of a book and its available files."""
    async with get_connection() as conn:
        book_cursor = await conn.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,)
        )
        book_row = await book_cursor.fetchone()
        if not book_row:
            return None

        files_cursor = await conn.execute(
            """
            SELECT id, format, file_id, file_size, file_name, duration
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


async def get_file_by_id(file_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves a specific file record for delivery."""
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


async def register_or_update_user(user_id: int, username: Optional[str] = None) -> None:
    """Inserts a user if new or updates their username."""
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


async def increment_user_downloads(user_id: int) -> None:
    """Increments the total download counter for a user."""
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


async def get_stats() -> Dict[str, Any]:
    """Computes administrative metrics."""
    async with get_connection() as conn:
        # Total books
        b_cur = await conn.execute("SELECT COUNT(*) AS total FROM books")
        total_books = (await b_cur.fetchone())["total"]

        # Format breakdown
        f_cur = await conn.execute(
            """
            SELECT format, COUNT(*) AS count
            FROM book_files
            GROUP BY format
            """
        )
        format_counts = {row["format"]: row["count"] for row in await f_cur.fetchall()}

        # Total registered users
        u_cur = await conn.execute("SELECT COUNT(*) AS total FROM users")
        total_users = (await u_cur.fetchone())["total"]

        # Total downloads delivered
        d_cur = await conn.execute("SELECT SUM(total_downloads) AS total FROM users")
        row = await d_cur.fetchone()
        total_downloads = row["total"] if row and row["total"] is not None else 0

        return {
            "total_books": total_books,
            "format_counts": format_counts,
            "total_users": total_users,
            "total_downloads": total_downloads,
        }
