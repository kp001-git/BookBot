import os
import unittest
import asyncio
import tempfile

import sqlite3
from utils.parser import parse_media_metadata, detect_format, clean_text_noise
from utils.enricher import format_duration, format_size
from config import Settings
from database.connection import (
    set_db_path,
    set_database_url,
    normalize_postgres_url_for_asyncpg,
    to_asyncpg_scheme,
    is_postgres,
    get_db_engine_name
)
from database.backup import create_sqlite_backup_file
from handlers.admin import get_uptime_duration
from database.models import (
    init_db,
    upsert_book_and_file,
    search_books,
    get_book_by_id,
    register_or_update_user,
    increment_user_downloads,
    get_stats
)
from handlers.search import (
    format_button_label,
    format_display_badges,
    render_book_card,
    is_archive_file
)
from handlers.inline import format_inline_formats


class TestPipeline(unittest.TestCase):
    def test_parser_noise_and_formats(self):
        # Test Case 1: Audio with channel promo and brackets
        m1 = parse_media_metadata(
            file_name="[L&A]. RAISED BY A SERIAL KILLER April Balascio .@heyuqt.m4b"
        )
        self.assertEqual(m1.format_type, "AUDIO")
        self.assertEqual(m1.clean_title, "Raised by a Serial Killer")
        self.assertEqual(m1.clean_author, "April Balascio")

        # Test Case 2: PDF with hyphen separation
        m2 = parse_media_metadata(
            file_name="[L&A]. Wariner Ruth - The Sound of Gravel .@heyuqt.pdf"
        )
        self.assertEqual(m2.format_type, "PDF")
        self.assertEqual(m2.clean_title, "The Sound of Gravel")
        self.assertEqual(m2.clean_author, "Wariner Ruth")

        # Test Case 3: EPUB standard formatting
        m3 = parse_media_metadata(
            file_name="Brandon Sanderson - Mistborn.epub"
        )
        self.assertEqual(m3.format_type, "EPUB")
        self.assertEqual(m3.clean_title, "Mistborn")
        self.assertEqual(m3.clean_author, "Brandon Sanderson")

        # Test Case 4: Native Telegram Audio with tags
        m4 = parse_media_metadata(
            audio_title="The Way of Kings",
            audio_performer="Brandon Sanderson",
            is_audio_msg=True
        )
        self.assertEqual(m4.format_type, "AUDIO")
        self.assertEqual(m4.clean_title, "The Way of Kings")
        self.assertEqual(m4.clean_author, "Brandon Sanderson")

        # Test Case 5: Archive reclassification to AUDIO (Audiobook)
        m5_zip = detect_format("MultiTrack_Book.zip")
        self.assertEqual(m5_zip, "AUDIO")

        m5_rar = detect_format("Complete_Audiobook.rar")
        self.assertEqual(m5_rar, "AUDIO")

        m5_tar = detect_format("Audiobook_Parts.tar")
        self.assertEqual(m5_tar, "AUDIO")

    def test_archive_button_and_badge_labels(self):
        # 1. Archive file button label
        btn_zip = format_button_label({"format": "AUDIO", "file_name": "book_audio.zip", "file_size": 10485760})
        self.assertIn("Audiobook (ZIP)", btn_zip)
        self.assertNotIn("ZIP Archive", btn_zip)

        btn_rar = format_button_label({"format": "ZIP", "file_name": "book_audio.rar", "file_size": 52428800})
        self.assertIn("Audiobook (RAR)", btn_rar)

        # 2. Native audio button label
        btn_m4b = format_button_label({"format": "AUDIO", "file_name": "book.m4b", "file_size": 104857600})
        self.assertIn("Audiobook (.m4b)", btn_m4b)

        # 3. EPUB and PDF
        btn_epub = format_button_label({"format": "EPUB", "file_name": "book.epub", "file_size": 2097152})
        self.assertIn("EPUB", btn_epub)

        # 4. Format badges in search and inline mode
        badges = format_display_badges(["AUDIO", "ZIP", "EPUB"])
        self.assertEqual(badges, "Audiobook, EPUB")
        self.assertNotIn("ZIP", badges)

        inline_fmts = format_inline_formats(["ZIP", "RAR", "PDF"])
        self.assertEqual(inline_fmts, "Audiobook, PDF")
        self.assertNotIn("ZIP", inline_fmts)
        self.assertNotIn("RAR", inline_fmts)

        # 5. Archive file check for delivery
        self.assertTrue(is_archive_file("book.zip", "AUDIO"))
        self.assertTrue(is_archive_file("book.rar", "ZIP"))
        self.assertFalse(is_archive_file("book.m4b", "AUDIO"))
        self.assertFalse(is_archive_file("book.pdf", "PDF"))

    def test_book_card_rendering_and_caption_limits(self):
        sample_book = {
            "id": 1,
            "clean_title": "Project Hail Mary",
            "clean_author": "Andy Weir",
            "cover_url": "https://books.google.com/sample_cover.jpg",
            "description": "Ryland Grace is the sole survivor on a desperate, last-chance mission—and if he fails, humanity and the earth itself will perish." * 5,
            "rating": 4.8,
            "year": "2021",
            "genres": "Science Fiction",
            "files": [
                {"id": 101, "format": "AUDIO", "file_name": "Project_Hail_Mary.m4b", "file_size": 500000000},
                {"id": 102, "format": "AUDIO", "file_name": "Project_Hail_Mary_MP3s.zip", "file_size": 450000000},
                {"id": 103, "format": "EPUB", "file_name": "Project_Hail_Mary.epub", "file_size": 2500000}
            ]
        }

        card = render_book_card(sample_book, query_id=1, page=1)
        self.assertEqual(card.cover_url, "https://books.google.com/sample_cover.jpg")
        # Ensure photo caption strictly respects Telegram's 1024-character limit
        self.assertLess(len(card.caption_text), 1024)
        # Check buttons
        keyboard = card.markup.inline_keyboard
        # Formats should be reclassified
        flat_buttons = [btn.text for row in keyboard for btn in row]
        self.assertTrue(any("Audiobook (.m4b)" in t for t in flat_buttons))
        self.assertTrue(any("Audiobook (ZIP)" in t for t in flat_buttons))
        self.assertTrue(any("EPUB" in t for t in flat_buttons))
        self.assertTrue(any("Back to Results" in t for t in flat_buttons))

    def test_database_and_fts5_workflow(self):
        async def run_db_tests():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                temp_db = f.name

            try:
                set_db_path(temp_db)
                await init_db()

                # Ingest Book 1 (Audiobook format)
                b1_id, is_new1, f1 = await upsert_book_and_file(
                    clean_title="Mistborn: The Final Empire",
                    clean_author="Brandon Sanderson",
                    canonical_title="mistborn the final empire",
                    canonical_author="brandon sanderson",
                    format_type="AUDIO",
                    file_id="audio_file_123",
                    file_size=500000000,
                    file_name="Mistborn.m4b",
                    duration=72000
                )
                self.assertTrue(is_new1)
                self.assertTrue(f1)

                # Ingest Book 1 (EPUB format) - should group under the same book ID
                b2_id, is_new2, f2 = await upsert_book_and_file(
                    clean_title="Mistborn: The Final Empire",
                    clean_author="Brandon Sanderson",
                    canonical_title="mistborn the final empire",
                    canonical_author="brandon sanderson",
                    format_type="EPUB",
                    file_id="epub_file_456",
                    file_size=2000000,
                    file_name="Mistborn.epub"
                )
                self.assertEqual(b1_id, b2_id)
                self.assertFalse(is_new2)
                self.assertTrue(f2)

                # Ingest Book 2
                b3_id, is_new3, f3 = await upsert_book_and_file(
                    clean_title="The Way of Kings",
                    clean_author="Brandon Sanderson",
                    canonical_title="the way of kings",
                    canonical_author="brandon sanderson",
                    format_type="PDF",
                    file_id="pdf_file_789",
                    file_size=5000000,
                    file_name="The Way of Kings.pdf"
                )
                self.assertNotEqual(b1_id, b3_id)

                # Fetch book by ID
                book = await get_book_by_id(b1_id)
                self.assertIsNotNone(book)
                self.assertEqual(len(book["files"]), 2)
                formats = [f["format"] for f in book["files"]]
                self.assertIn("AUDIO", formats)
                self.assertIn("EPUB", formats)

                # FTS5 Sub-second search
                results, total = await search_books("Mistborn")
                self.assertEqual(total, 1)
                self.assertEqual(results[0]["clean_title"], "Mistborn: The Final Empire")

                # Author search
                results, total = await search_books("Sanderson")
                self.assertEqual(total, 2)

                # User tracking & downloads
                await register_or_update_user(1001, "reader_john")
                await increment_user_downloads(1001)
                await increment_user_downloads(1001)

                stats = await get_stats()
                self.assertEqual(stats["total_books"], 2)
                self.assertEqual(stats["format_counts"]["AUDIO"], 1)
                self.assertEqual(stats["format_counts"]["EPUB"], 1)
                self.assertEqual(stats["format_counts"]["PDF"], 1)
                self.assertEqual(stats["total_users"], 1)
                self.assertEqual(stats["total_downloads"], 2)

            finally:
                if os.path.exists(temp_db):
                    os.remove(temp_db)

        asyncio.run(run_db_tests())

    def test_database_url_normalization_and_schemes(self):
        # 1. URL normalization for asyncpg
        url1 = "postgres://user:pass@ep-test.neon.tech/neondb?sslmode=require"
        normalized1 = normalize_postgres_url_for_asyncpg(url1)
        self.assertTrue(normalized1.startswith("postgresql://"))

        url2 = "postgresql+asyncpg://user:pass@localhost:5432/testdb"
        normalized2 = normalize_postgres_url_for_asyncpg(url2)
        self.assertTrue(normalized2.startswith("postgresql://"))

        # 2. Convert to asyncpg scheme
        asyncpg_url = to_asyncpg_scheme("postgres://user:pass@localhost/db")
        self.assertTrue(asyncpg_url.startswith("postgresql+asyncpg://"))

        # 3. Switching active engine
        set_database_url("postgres://user:pass@localhost/db")
        self.assertTrue(is_postgres())
        self.assertEqual(get_db_engine_name(), "PostgreSQL")

        # Switching back to SQLite
        set_db_path("data/library.db")
        self.assertFalse(is_postgres())
        self.assertEqual(get_db_engine_name(), "SQLite FTS5")

    def test_sqlite_backup_creation(self):
        async def run_backup_test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                temp_db = f.name

            try:
                set_db_path(temp_db)
                await init_db()

                await upsert_book_and_file(
                    clean_title="Backup Test Title",
                    clean_author="Backup Author",
                    canonical_title="backup test title",
                    canonical_author="backup author",
                    format_type="AUDIO",
                    file_id="backup_file_999",
                )

                backup_file = await create_sqlite_backup_file()
                try:
                    self.assertTrue(os.path.exists(backup_file))
                    # Verify backup contains the book
                    bck_conn = sqlite3.connect(backup_file)
                    cursor = bck_conn.cursor()
                    cursor.execute("SELECT clean_title FROM books WHERE clean_title = 'Backup Test Title'")
                    row = cursor.fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row[0], "Backup Test Title")
                    bck_conn.close()
                finally:
                    if os.path.exists(backup_file):
                        os.remove(backup_file)
            finally:
                if os.path.exists(temp_db):
                    os.remove(temp_db)

        asyncio.run(run_backup_test())

    def test_admin_uptime_and_stats_display(self):
        uptime = get_uptime_duration()
        self.assertTrue(any(u in uptime for u in ["s", "m", "h", "d"]))

    def test_config_credentials_and_validation(self):
        test_settings = Settings(
            BOT_TOKEN="123456:FAKE_TOKEN_FOR_TESTING",
            STORAGE_CHANNEL_ID="-1001234567890",
            UPDATES_CHANNEL_ID="-1009876543210",
            DATABASE_URL="   "
        )
        self.assertEqual(test_settings.STORAGE_CHANNEL_ID, -1001234567890)
        self.assertEqual(test_settings.UPDATES_CHANNEL_ID, -1009876543210)
        self.assertIsNone(test_settings.DATABASE_URL)

    def test_developer_credits_and_attribution(self):
        from unittest.mock import AsyncMock, MagicMock
        from handlers.search import handle_help, handle_start

        async def run_attribution_test():
            msg_help = MagicMock()
            msg_help.answer = AsyncMock()

            await handle_help(msg_help)

            msg_help.answer.assert_called_once()
            args, kwargs = msg_help.answer.call_args
            text = args[0]
            markup = kwargs.get("reply_markup")

            self.assertIn("https://t.me/souldumpp", text)
            self.assertIn("Soul", text)
            self.assertIsNotNone(markup)
            button = markup.inline_keyboard[0][0]
            self.assertEqual(button.url, "https://t.me/souldumpp")
            self.assertEqual(button.text, "Developer")

            # Test /start welcome message and button
            msg_start = MagicMock()
            msg_start.from_user = None
            msg_start.answer = AsyncMock()
            cmd_start = MagicMock()
            cmd_start.args = None

            await handle_start(msg_start, cmd_start)

            msg_start.answer.assert_called_once()
            args_s, kwargs_s = msg_start.answer.call_args
            text_s = args_s[0]
            markup_s = kwargs_s.get("reply_markup")

            self.assertIn("https://t.me/souldumpp", text_s)
            self.assertIn("Soul", text_s)
            self.assertIsNotNone(markup_s)
            button_s = markup_s.inline_keyboard[0][0]
            self.assertEqual(button_s.url, "https://t.me/souldumpp")
            self.assertEqual(button_s.text, "Developer")

        asyncio.run(run_attribution_test())


if __name__ == "__main__":
    unittest.main()
