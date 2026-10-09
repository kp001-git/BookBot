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

    def test_storage_channel_id_flexible_validation(self):
        from handlers.channel import is_storage_channel
        from config import settings

        orig = settings.STORAGE_CHANNEL_ID
        try:
            settings.STORAGE_CHANNEL_ID = -1004392683191

            # Exact int
            self.assertTrue(is_storage_channel(-1004392683191))
            # String representation
            self.assertTrue(is_storage_channel("-1004392683191"))
            # Unprefixed representation
            self.assertTrue(is_storage_channel("4392683191"))
            self.assertTrue(is_storage_channel(4392683191))
            # Mismatched ID
            self.assertFalse(is_storage_channel(-1001111111111))
            self.assertFalse(is_storage_channel("999999999"))
        finally:
            settings.STORAGE_CHANNEL_ID = orig

    def test_models_and_config_aliases(self):
        from config import config, settings
        from database.models import insert_or_update_book, upsert_book_and_file

        self.assertIs(config, settings)
        self.assertIs(insert_or_update_book, upsert_book_and_file)

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

    def test_fuzzy_search_fallback(self):
        from database.models import is_file_indexed

        async def run_fuzzy_test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                temp_db = f.name
            try:
                set_db_path(temp_db)
                await init_db()

                await upsert_book_and_file(
                    clean_title="Atomic Habits",
                    clean_author="James Clear",
                    canonical_title="atomic habits",
                    canonical_author="james clear",
                    format_type="EPUB",
                    file_id="epub_atomic_123",
                    file_size=2000000,
                    channel_message_id=42
                )

                # 1. Exact search matches via FTS5
                exact_results, count = await search_books("Atomic Habits")
                self.assertEqual(count, 1)
                self.assertEqual(exact_results[0]["clean_title"], "Atomic Habits")

                # 2. Typo query fails in FTS5 prefix match but catches in rapidfuzz fallback
                fuzzy_results, f_count = await search_books("Atmoic Habits")
                self.assertGreaterEqual(f_count, 1)
                self.assertEqual(fuzzy_results[0]["clean_title"], "Atomic Habits")

                # 3. Check is_file_indexed
                self.assertTrue(await is_file_indexed(channel_message_id=42))
                self.assertTrue(await is_file_indexed(file_id="epub_atomic_123"))
                self.assertFalse(await is_file_indexed(channel_message_id=9999))
            finally:
                if os.path.exists(temp_db):
                    os.remove(temp_db)

        asyncio.run(run_fuzzy_test())

    def test_delivery_copy_message_with_fallback(self):
        from unittest.mock import AsyncMock, MagicMock
        from handlers.search import handle_download_callback
        from config import settings

        async def run_delivery_test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                temp_db = f.name
            try:
                set_db_path(temp_db)
                await init_db()

                b_id, _, _ = await upsert_book_and_file(
                    clean_title="Delivery Test Book",
                    clean_author="Test Author",
                    canonical_title="delivery test book",
                    canonical_author="test author",
                    format_type="AUDIO",
                    file_id="audio_file_deliv",
                    file_size=1000000,
                    channel_message_id=555
                )

                # Fetch file record ID
                book = await get_book_by_id(b_id)
                file_row_id = book["files"][0]["id"]

                # Case A: copy_message succeeds
                bot_mock = MagicMock()
                bot_mock.copy_message = AsyncMock()
                bot_mock.send_audio = AsyncMock()
                bot_mock.send_document = AsyncMock()

                cb_mock = MagicMock()
                cb_mock.data = f"dl:{file_row_id}"
                cb_mock.from_user.id = 99999
                cb_mock.answer = AsyncMock()
                cb_mock.message = None

                await handle_download_callback(cb_mock, bot_mock)

                bot_mock.copy_message.assert_called_once_with(
                    chat_id=99999,
                    from_chat_id=settings.STORAGE_CHANNEL_ID,
                    message_id=555
                )
                bot_mock.send_audio.assert_not_called()

                # Case B: copy_message fails -> fallback to direct send
                bot_mock.reset_mock()
                bot_mock.copy_message.side_effect = Exception("Message deleted in channel")

                await handle_download_callback(cb_mock, bot_mock)

                bot_mock.copy_message.assert_called_once()
                bot_mock.send_audio.assert_called_once()
            finally:
                if os.path.exists(temp_db):
                    os.remove(temp_db)

        asyncio.run(run_delivery_test())

    def test_insert_or_update_book_with_cover_and_description(self):
        from database.connection import set_db_path
        from database.models import init_db, insert_or_update_book, get_book_by_id

        async def run_insert_test():
            temp_db = "scratch/test_insert_cover.db"
            os.makedirs("scratch", exist_ok=True)
            set_db_path(temp_db)
            await init_db()

            try:
                # Direct call with cover_url=None, description=None
                book_id, is_new_b, is_new_f = await insert_or_update_book(
                    clean_title="Wal Mode Book",
                    clean_author="Wal Author",
                    canonical_title="wal mode book",
                    canonical_author="wal author",
                    format_type="AUDIO",
                    file_id="wal_file_123",
                    cover_url=None,
                    description=None,
                    channel_message_id=9876
                )
                self.assertTrue(is_new_b)
                self.assertTrue(is_new_f)

                book = await get_book_by_id(book_id)
                self.assertIsNotNone(book)
                self.assertEqual(book["clean_title"], "Wal Mode Book")
                self.assertIsNone(book["cover_url"])
                self.assertIsNone(book["description"])
            finally:
                if os.path.exists(temp_db):
                    os.remove(temp_db)

        asyncio.run(run_insert_test())

    def test_backfill_cli_arguments_parsing(self):
        import argparse
        from utils.backfill import main

        # Simulate argparse setup verification
        parser = argparse.ArgumentParser()
        parser.add_argument("--skip-enrich", action="store_true", default=True)
        parser.add_argument("--enrich", dest="skip_enrich", action="store_false")
        parser.add_argument("--no-enrich", dest="skip_enrich", action="store_true")

        # Default is True
        args = parser.parse_args([])
        self.assertTrue(args.skip_enrich)

        # --enrich sets to False
        args = parser.parse_args(["--enrich"])
        self.assertFalse(args.skip_enrich)

        # --skip-enrich sets to True
        args = parser.parse_args(["--skip-enrich"])
        self.assertTrue(args.skip_enrich)

        # --no-enrich sets to True
        args = parser.parse_args(["--no-enrich"])
        self.assertTrue(args.skip_enrich)

    def test_backfill_run_skips_external_enrichment(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        from utils.backfill import run_backfill

        async def run_test():
            temp_db = "scratch/test_backfill_mock.db"
            os.makedirs("scratch", exist_ok=True)

            mock_enrich = AsyncMock()

            # Mock Telethon client
            mock_client = MagicMock()
            mock_client.start = AsyncMock()
            mock_client.disconnect = AsyncMock()
            mock_client.get_dialogs = AsyncMock(return_value=[])
            target_entity = MagicMock(id=123, title="Target Storage")
            mock_client.get_entity = AsyncMock(return_value=target_entity)

            # Mock message with audio document
            from telethon.tl.types import Document, DocumentAttributeFilename, DocumentAttributeAudio
            doc = MagicMock(spec=Document)
            doc.id = 99991111
            doc.size = 12345
            doc.mime_type = "audio/mpeg"
            doc.attributes = [
                DocumentAttributeFilename(file_name="Deep Work - Cal Newport.mp3"),
                DocumentAttributeAudio(duration=1800, title="Deep Work", performer="Cal Newport")
            ]

            mock_msg = MagicMock()
            mock_msg.id = 101
            mock_msg.media = doc
            mock_msg.document = doc
            mock_msg.file = None
            mock_msg.message = "Deep Work by Cal Newport"
            mock_msg.raw_text = "Deep Work by Cal Newport"

            async def mock_iter(entity, limit=None):
                yield mock_msg

            mock_client.iter_messages = mock_iter

            with patch("utils.backfill.TelegramClient", return_value=mock_client), \
                 patch("utils.enricher.enrich_book_metadata", mock_enrich):
                metrics = await run_backfill(
                    api_id=12345,
                    api_hash="fakehash",
                    channel_id=-1004392683191,
                    session_name="test_session",
                    skip_enrich=True,
                    limit=1,
                    db_path=temp_db
                )

                # Enrichment MUST NOT be called
                mock_enrich.assert_not_called()
                self.assertEqual(metrics["scanned"], 1)
                self.assertEqual(metrics["indexed_new_files"], 1)

            if os.path.exists(temp_db):
                os.remove(temp_db)

        asyncio.run(run_test())

    def test_migrate_sqlite_to_pg_helpers(self):
        from utils.migrate_sqlite_to_pg import parse_timestamp
        from datetime import datetime

        self.assertIsNone(parse_timestamp(None))
        self.assertIsNone(parse_timestamp(""))
        dt_str = "2026-10-05 16:52:56"
        dt = parse_timestamp(dt_str)
        self.assertIsInstance(dt, datetime)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 5)
        self.assertEqual(dt.hour, 16)
        self.assertEqual(dt.minute, 52)
        self.assertEqual(dt.second, 56)


if __name__ == "__main__":
    unittest.main()



