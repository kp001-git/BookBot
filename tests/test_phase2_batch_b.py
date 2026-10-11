import os
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from config import settings
from database.connection import set_db_path, close_db
from database.models import (
    init_db,
    upsert_book_and_file,
    get_book_by_id,
    get_user_role,
    set_user_role,
    toggle_favorite,
    is_favorite,
    get_user_favorites,
    create_request,
    update_request_status,
    get_requests,
    update_book_metadata,
    delete_book_file,
    get_all_user_ids,
    register_or_update_user
)
from services.search import add_or_update_catalog_book, catalog_cache


class TestPhase2BatchB(unittest.IsolatedAsyncioTestCase):
    """Unit and integration tests for Phase 2 Batch B features."""

    async def asyncSetUp(self):
        self.test_db = f"test_phase2_batch_b_{os.getpid()}.db"
        set_db_path(self.test_db)
        await init_db()

    async def asyncTearDown(self):
        await close_db()
        if os.path.exists(self.test_db):
            try:
                os.remove(self.test_db)
            except Exception:
                pass

    async def test_rbac_resolution(self):
        """Tests that roles resolve correctly, with ADMIN_IDS defaulting to 'admin'."""
        admin_id = 999999
        with patch.object(settings, "ADMIN_IDS", [admin_id]):
            # Admin ID resolves to admin even without explicit role in DB
            self.assertEqual(await get_user_role(admin_id), "admin")

            # Regular user resolves to 'user'
            regular_id = 12345
            self.assertEqual(await get_user_role(regular_id), "user")

            # Promote regular_id to moderator
            await set_user_role(regular_id, "moderator")
            self.assertEqual(await get_user_role(regular_id), "moderator")

            # Promote regular_id to admin
            await set_user_role(regular_id, "admin")
            self.assertEqual(await get_user_role(regular_id), "admin")

    async def test_favorites_toggle_and_retrieval(self):
        """Tests bookmarking and retrieving user favorite books."""
        book_id, _, _ = await upsert_book_and_file(
            clean_title="Dune",
            clean_author="Frank Herbert",
            canonical_title="dune",
            canonical_author="frank herbert",
            format_type="EPUB",
            file_id="tg_file_dune_epub"
        )
        user_id = 555001

        # Initially not a favorite
        self.assertFalse(await is_favorite(user_id, book_id))

        # Toggle favorite -> True
        added = await toggle_favorite(user_id, book_id)
        self.assertTrue(added)
        self.assertTrue(await is_favorite(user_id, book_id))

        favs = await get_user_favorites(user_id)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0]["id"], book_id)
        self.assertEqual(favs[0]["clean_title"], "Dune")

        # Toggle favorite -> False (removed)
        removed = await toggle_favorite(user_id, book_id)
        self.assertFalse(removed)
        self.assertFalse(await is_favorite(user_id, book_id))
        self.assertEqual(len(await get_user_favorites(user_id)), 0)

    async def test_community_request_desk(self):
        """Tests creating, fetching, and updating community book requests."""
        user_id = 777002
        req_id = await create_request(user_id, "Project Hail Mary by Andy Weir")
        self.assertIsInstance(req_id, int)

        # Check pending requests
        pending = await get_requests(status="pending")
        self.assertTrue(any(r["id"] == req_id for r in pending))

        # Mark request as fulfilled
        updated = await update_request_status(req_id, "fulfilled")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], "fulfilled")
        self.assertEqual(updated["query"], "Project Hail Mary by Andy Weir")

        # Pending should now exclude it
        pending_after = await get_requests(status="pending")
        self.assertFalse(any(r["id"] == req_id for r in pending_after))

    async def test_catalog_editor_metadata_and_file_deletion(self):
        """Tests editing book title/author and deleting specific file record."""
        book_id, _, _ = await upsert_book_and_file(
            clean_title="Old Title",
            clean_author="Old Author",
            canonical_title="old title",
            canonical_author="old author",
            format_type="AUDIO",
            file_id="tg_file_audio_123"
        )
        book = await get_book_by_id(book_id)
        self.assertIsNotNone(book)
        self.assertEqual(book["clean_title"], "Old Title")
        self.assertEqual(len(book["files"]), 1)
        file_record_id = book["files"][0]["id"]

        # Rename title and author
        await update_book_metadata(book_id, title="New Title", author="New Author")
        book_after = await get_book_by_id(book_id)
        self.assertEqual(book_after["clean_title"], "New Title")
        self.assertEqual(book_after["clean_author"], "New Author")

        # In-memory search sync
        add_or_update_catalog_book(book_id, "New Title", "New Author")
        results, count = catalog_cache.search("New Title")
        self.assertTrue(any(r["id"] == book_id for r in results))

        # Delete specific file
        del_result = await delete_book_file(file_record_id)
        self.assertTrue(del_result)

        book_files_after = await get_book_by_id(book_id)
        self.assertEqual(len(book_files_after["files"]), 0)

    async def test_get_all_user_ids_for_broadcaster(self):
        """Tests fetching all users for safe broadcaster."""
        await register_or_update_user(1001, "user1")
        await register_or_update_user(1002, "user2")
        await register_or_update_user(1003, "user3")

        uids = await get_all_user_ids()
        self.assertIn(1001, uids)
        self.assertIn(1002, uids)
        self.assertIn(1003, uids)


if __name__ == "__main__":
    unittest.main()
