import os
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from config import settings
from database.connection import set_db_path, close_db
from database.models import init_db, upsert_book_and_file, get_requests
from filters.group_topic import IsAllowedGroupTopic, GroupCommandFilter
from handlers.group_search import handle_group_search
from handlers.group_request import handle_group_request
from services.search import catalog_cache, add_or_update_catalog_book


class TestPhase2GroupTopicAndDMRedirection(unittest.IsolatedAsyncioTestCase):
    """Tests for Phase 2.2: Group Forum Topics, Zero-Spam Triggering & DM Redirection."""

    async def asyncSetUp(self):
        self.test_db = f"test_phase2_groups_{os.getpid()}.db"
        set_db_path(self.test_db)
        await init_db()
        catalog_cache.clear()

    async def asyncTearDown(self):
        await close_db()
        catalog_cache.clear()
        if os.path.exists(self.test_db):
            try:
                os.remove(self.test_db)
            except Exception:
                pass

    async def test_group_topic_filter(self):
        """Validates chat type and forum topic restrictions."""
        topic_filter = IsAllowedGroupTopic()

        # 1. Private chat -> rejected
        msg_private = MagicMock()
        msg_private.chat = MagicMock(type="private", is_forum=False)
        self.assertFalse(await topic_filter(msg_private))

        # 2. Channel -> rejected
        msg_channel = MagicMock()
        msg_channel.chat = MagicMock(type="channel", is_forum=False)
        self.assertFalse(await topic_filter(msg_channel))

        # 3. Standard Group / Supergroup -> allowed
        msg_group = MagicMock()
        msg_group.chat = MagicMock(type="supergroup", is_forum=False)
        self.assertTrue(await topic_filter(msg_group))

        # 4. Forum Supergroup - General topic (thread_id = None or 1) -> allowed
        msg_forum_general_none = MagicMock()
        msg_forum_general_none.chat = MagicMock(type="supergroup", is_forum=True)
        msg_forum_general_none.message_thread_id = None
        self.assertTrue(await topic_filter(msg_forum_general_none))

        msg_forum_general_1 = MagicMock()
        msg_forum_general_1.chat = MagicMock(type="supergroup", is_forum=True)
        msg_forum_general_1.message_thread_id = 1
        self.assertTrue(await topic_filter(msg_forum_general_1))

        # 5. Forum Supergroup - Other topic (thread_id = 2, 42) -> rejected silently
        msg_forum_other = MagicMock()
        msg_forum_other.chat = MagicMock(type="supergroup", is_forum=True)
        msg_forum_other.message_thread_id = 42
        self.assertFalse(await topic_filter(msg_forum_other))

    async def test_group_command_filter(self):
        """Validates command matching for /search, #search, /request, #request with optional bot suffix."""
        search_filter = GroupCommandFilter(["search"])
        request_filter = GroupCommandFilter(["request"])

        # /search query
        msg1 = MagicMock(text="/search Dune", caption=None)
        res1 = await search_filter(msg1)
        self.assertEqual(res1, {"command_name": "search", "query": "Dune"})

        # /search@AudioSoulBot query
        msg2 = MagicMock(text="/search@AudioSoulBot The Hobbit", caption=None)
        res2 = await search_filter(msg2)
        self.assertEqual(res2, {"command_name": "search", "query": "The Hobbit"})

        # #search query
        msg3 = MagicMock(text="#search Foundation", caption=None)
        res3 = await search_filter(msg3)
        self.assertEqual(res3, {"command_name": "search", "query": "Foundation"})

        # /request query
        msg4 = MagicMock(text="/request Project Hail Mary", caption=None)
        res4 = await request_filter(msg4)
        self.assertEqual(res4, {"command_name": "request", "query": "Project Hail Mary"})

        # #request query
        msg5 = MagicMock(text="#request Clean Code", caption=None)
        res5 = await request_filter(msg5)
        self.assertEqual(res5, {"command_name": "request", "query": "Clean Code"})

        # /request with no query
        msg6 = MagicMock(text="/request", caption=None)
        res6 = await request_filter(msg6)
        self.assertEqual(res6, {"command_name": "request", "query": ""})

        # Arbitrary conversational text -> ignored
        msg_chat = MagicMock(text="Hey everyone, did anyone read Dune?", caption=None)
        self.assertFalse(await search_filter(msg_chat))
        self.assertFalse(await request_filter(msg_chat))

    async def test_group_search_found_results(self):
        """Tests group search returning compact card with deep link buttons and disabled preview."""
        # Index books into cache
        book_id, _, _ = await upsert_book_and_file(
            clean_title="Dune",
            clean_author="Frank Herbert",
            canonical_title="dune",
            canonical_author="frank herbert",
            format_type="AUDIO",
            file_id="audio_file_1"
        )
        add_or_update_catalog_book(book_id, "Dune", "Frank Herbert", formats=["AUDIO"])

        msg = MagicMock()
        msg.reply = AsyncMock()
        bot_mock = MagicMock()

        with patch.object(settings, "BOT_USERNAME", "AudioSoulBot"):
            await handle_group_search(msg, query="Dune", bot=bot_mock)

        msg.reply.assert_called_once()
        args, kwargs = msg.reply.call_args
        text = args[0]
        markup = kwargs.get("reply_markup")
        link_options = kwargs.get("link_preview_options")

        self.assertIn("Found 1 result(s)", text)
        self.assertIn("Dune", text)
        self.assertIsNotNone(markup)
        self.assertTrue(link_options.is_disabled)

        # Verify button deep link
        button = markup.inline_keyboard[0][0]
        self.assertIn("Get Dune in Bot", button.text)
        self.assertEqual(button.url, f"https://t.me/AudioSoulBot?start=book_{book_id}")

    async def test_group_search_not_found(self):
        """Tests clean not-found message in group with link to bot DM."""
        msg = MagicMock()
        msg.reply = AsyncMock()
        bot_mock = MagicMock()

        with patch.object(settings, "BOT_USERNAME", "AudioSoulBot"):
            await handle_group_search(msg, query="NonExistentBookXYZ", bot=bot_mock)

        msg.reply.assert_called_once()
        args, kwargs = msg.reply.call_args
        text = args[0]
        markup = kwargs.get("reply_markup")
        link_options = kwargs.get("link_preview_options")

        self.assertIn("Book not found in library", text)
        self.assertIn("/request", text)
        self.assertTrue(link_options.is_disabled)

        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.url, "https://t.me/AudioSoulBot?start=request")

    async def test_group_request_direct(self):
        """Tests /request <query> in group saving to DB and confirming."""
        msg = MagicMock()
        msg.reply = AsyncMock()
        msg.from_user = MagicMock(id=112233, username="reader_bob")
        msg.reply_to_message = None
        bot_mock = MagicMock()

        await handle_group_request(msg, query="Steve Jobs by Walter Isaacson", bot=bot_mock)

        msg.reply.assert_called_once()
        args, kwargs = msg.reply.call_args
        text = args[0]
        link_options = kwargs.get("link_preview_options")

        self.assertIn("Request Received!", text)
        self.assertIn("Steve Jobs by Walter Isaacson", text)
        self.assertTrue(link_options.is_disabled)

        # Verify DB entry
        requests = await get_requests(status="pending")
        self.assertTrue(any(r["query"] == "Steve Jobs by Walter Isaacson" for r in requests))

    async def test_group_request_via_reply(self):
        """Tests /request in reply to a message extracting title and saving to DB."""
        replied_msg = MagicMock()
        replied_msg.text = "Can someone upload Sapiens by Yuval Noah Harari?"
        replied_msg.caption = None

        msg = MagicMock()
        msg.reply = AsyncMock()
        msg.from_user = MagicMock(id=445566, username="alice")
        msg.reply_to_message = replied_msg
        bot_mock = MagicMock()

        await handle_group_request(msg, query="", bot=bot_mock)

        msg.reply.assert_called_once()
        args, kwargs = msg.reply.call_args
        text = args[0]
        self.assertIn("Request Received!", text)
        self.assertIn("Can someone upload Sapiens", text)

        # Verify DB entry
        requests = await get_requests(status="pending")
        self.assertTrue(any("Sapiens" in r["query"] for r in requests))


if __name__ == "__main__":
    unittest.main()
