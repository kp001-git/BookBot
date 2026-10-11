import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import Settings, settings
from middlewares.subscription import (
    check_user_subscription,
    build_subscription_markup,
    get_subscription_gate_message
)
from handlers.start import (
    handle_start,
    handle_verify_subscription,
    WELCOME_TEXT
)
from handlers.search import render_book_card
from handlers.inline import handle_inline_query
from services.search import catalog_cache, add_or_update_catalog_book


class TestPhase2AudienceGrowth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        catalog_cache.clear()

    def tearDown(self):
        catalog_cache.clear()

    def test_required_channels_configuration(self):
        # 1. Default channels
        s_default = Settings(
            BOT_TOKEN="1234:dummy",
            STORAGE_CHANNEL_ID=-100123
        )
        self.assertEqual(s_default.REQUIRED_CHANNELS, ["@souldumpp", "@Liora_BookHouse"])

        # 2. Comma-separated override
        s_custom = Settings(
            BOT_TOKEN="1234:dummy",
            STORAGE_CHANNEL_ID=-100123,
            REQUIRED_CHANNELS="@my_channel_1, @my_channel_2 , @my_channel_3"
        )
        self.assertEqual(
            s_custom.REQUIRED_CHANNELS,
            ["@my_channel_1", "@my_channel_2", "@my_channel_3"]
        )

        # 3. List override
        s_list = Settings(
            BOT_TOKEN="1234:dummy",
            STORAGE_CHANNEL_ID=-100123,
            REQUIRED_CHANNELS=["@chan_a", "@chan_b"]
        )
        self.assertEqual(s_list.REQUIRED_CHANNELS, ["@chan_a", "@chan_b"])

    async def test_admin_exemption(self):
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock()

        # Admin ID should immediately pass without calling get_chat_member
        admin_id = settings.ADMIN_IDS[0] if settings.ADMIN_IDS else 999999
        with patch.object(settings, "ADMIN_IDS", [admin_id]):
            is_sub, unsub = await check_user_subscription(bot_mock, admin_id)
            self.assertTrue(is_sub)
            self.assertEqual(unsub, [])
            bot_mock.get_chat_member.assert_not_called()

    async def test_subscription_check_statuses(self):
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock()

        user_id = 77777

        # Case 1: Member of all required channels
        bot_mock.get_chat_member.return_value = MagicMock(status="member")
        with patch.object(settings, "ADMIN_IDS", []), patch.object(settings, "REQUIRED_CHANNELS", ["@ch1", "@ch2"]):
            is_sub, unsub = await check_user_subscription(bot_mock, user_id)
            self.assertTrue(is_sub)
            self.assertEqual(unsub, [])
            self.assertEqual(bot_mock.get_chat_member.call_count, 2)

        # Case 2: Administrator and Creator
        bot_mock.reset_mock()
        bot_mock.get_chat_member.side_effect = [
            MagicMock(status="administrator"),
            MagicMock(status="creator")
        ]
        with patch.object(settings, "ADMIN_IDS", []), patch.object(settings, "REQUIRED_CHANNELS", ["@ch1", "@ch2"]):
            is_sub, unsub = await check_user_subscription(bot_mock, user_id)
            self.assertTrue(is_sub)
            self.assertEqual(unsub, [])

        # Case 3: One channel left, one member
        bot_mock.reset_mock()
        bot_mock.get_chat_member.side_effect = [
            MagicMock(status="member"),
            MagicMock(status="left")
        ]
        with patch.object(settings, "ADMIN_IDS", []), patch.object(settings, "REQUIRED_CHANNELS", ["@ch1", "@ch2"]):
            is_sub, unsub = await check_user_subscription(bot_mock, user_id)
            self.assertFalse(is_sub)
            self.assertEqual(unsub, ["@ch2"])

        # Case 4: TelegramBadRequest / user not found exception
        bot_mock.reset_mock()
        bot_mock.get_chat_member.side_effect = Exception("Bad Request: user not found")
        with patch.object(settings, "ADMIN_IDS", []), patch.object(settings, "REQUIRED_CHANNELS", ["@ch1", "@ch2"]):
            is_sub, unsub = await check_user_subscription(bot_mock, user_id)
            self.assertFalse(is_sub)
            self.assertEqual(unsub, ["@ch1", "@ch2"])

    def test_build_subscription_markup(self):
        with patch.object(settings, "REQUIRED_CHANNELS", ["@souldumpp", "@Liora_BookHouse"]):
            markup = build_subscription_markup("book:42")
            buttons = [b for row in markup.inline_keyboard for b in row]

            # Should have buttons for each channel and verify button
            self.assertEqual(len(buttons), 3)
            self.assertEqual(buttons[0].text, "📢 Join @souldumpp")
            self.assertEqual(buttons[0].url, "https://t.me/souldumpp")
            self.assertEqual(buttons[1].text, "📢 Join @Liora_BookHouse")
            self.assertEqual(buttons[1].url, "https://t.me/Liora_BookHouse")
            self.assertEqual(buttons[2].text, "🔄 Verify Membership")
            self.assertEqual(buttons[2].callback_data, "verify_sub:book:42")

    def test_share_link_in_render_book_card(self):
        sample_book = {
            "id": 105,
            "clean_title": "Project Hail Mary",
            "clean_author": "Andy Weir",
            "formats": ["AUDIO"],
            "files": [
                {"id": 201, "format": "AUDIO", "file_name": "HailMary.m4b", "file_size": 400000000}
            ]
        }
        with patch.object(settings, "BOT_USERNAME", "AudioSoulBot"):
            card = render_book_card(sample_book, query_id=0, page=1)
            buttons = [b for row in card.markup.inline_keyboard for b in row]
            share_btn = next((b for b in buttons if b.text == "🔗 Share Link"), None)
            self.assertIsNotNone(share_btn)
            self.assertEqual(share_btn.url, "https://t.me/AudioSoulBot?start=book_105")

    async def test_deep_link_start_gatekeeper_unsubscribed(self):
        # User requests /start book_42 while NOT subscribed
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))

        msg_mock = MagicMock()
        msg_mock.from_user.id = 55555
        msg_mock.from_user.username = "test_reader"
        msg_mock.answer = AsyncMock()

        cmd_mock = MagicMock()
        cmd_mock.args = "book_42"

        with patch.object(settings, "ADMIN_IDS", []), patch("handlers.start.register_or_update_user", AsyncMock()):
            await handle_start(msg_mock, cmd_mock, bot_mock)

            msg_mock.answer.assert_called_once()
            args, kwargs = msg_mock.answer.call_args
            text = args[0]
            markup: InlineKeyboardMarkup = kwargs["reply_markup"]

            self.assertIn("Subscription Required", text)
            verify_btn = next(b for row in markup.inline_keyboard for b in row if "Verify" in b.text)
            self.assertEqual(verify_btn.callback_data, "verify_sub:book:42")

    async def test_deep_link_start_subscribed_delivers_book(self):
        # User requests /start book_42 while subscribed
        bot_mock = MagicMock()
        bot_mock.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        msg_mock = MagicMock()
        msg_mock.from_user.id = 55555
        msg_mock.from_user.username = "test_reader"
        msg_mock.answer = AsyncMock()
        msg_mock.answer_photo = AsyncMock()

        cmd_mock = MagicMock()
        cmd_mock.args = "book_42"

        mock_book = {
            "id": 42,
            "clean_title": "Dune",
            "clean_author": "Frank Herbert",
            "cover_url": "https://example.com/dune.jpg",
            "files": []
        }

        with patch.object(settings, "ADMIN_IDS", []), \
             patch("handlers.start.register_or_update_user", AsyncMock()), \
             patch("handlers.start.get_book_by_id", AsyncMock(return_value=mock_book)), \
             patch("handlers.start.ensure_book_cover", AsyncMock()):

            await handle_start(msg_mock, cmd_mock, bot_mock)

            msg_mock.answer_photo.assert_called_once()
            kwargs = msg_mock.answer_photo.call_args[1]
            self.assertEqual(kwargs["photo"], "https://example.com/dune.jpg")
            self.assertIn("Dune", kwargs["caption"])

    async def test_verify_subscription_callback_flow(self):
        bot_mock = MagicMock()
        cb_mock = MagicMock()
        cb_mock.from_user.id = 55555
        cb_mock.data = "verify_sub:book_42"
        cb_mock.answer = AsyncMock()
        cb_mock.message = MagicMock()
        cb_mock.message.delete = AsyncMock()
        cb_mock.message.answer = AsyncMock()
        cb_mock.message.answer_photo = AsyncMock()

        # Step 1: Still not subscribed -> Alert prompt
        bot_mock.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))
        with patch.object(settings, "ADMIN_IDS", []):
            await handle_verify_subscription(cb_mock, bot_mock)
            cb_mock.answer.assert_called_once()
            self.assertTrue(cb_mock.answer.call_args[1].get("show_alert"))
            cb_mock.message.delete.assert_not_called()

        # Step 2: Now subscribed -> Deletes gatekeeper and delivers book 42
        cb_mock.answer.reset_mock()
        bot_mock.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        cb_mock.data = "verify_sub:book:42"

        mock_book = {
            "id": 42,
            "clean_title": "Dune",
            "clean_author": "Frank Herbert",
            "cover_url": None,
            "files": []
        }

        with patch.object(settings, "ADMIN_IDS", []), \
             patch("handlers.start.get_book_by_id", AsyncMock(return_value=mock_book)), \
             patch("handlers.start.ensure_book_cover", AsyncMock()):

            await handle_verify_subscription(cb_mock, bot_mock)

            cb_mock.message.delete.assert_called_once()
            cb_mock.message.answer.assert_called_once()
            text = cb_mock.message.answer.call_args[0][0]
            self.assertIn("Dune", text)

    async def test_inline_query_returns_up_to_20_with_deep_links(self):
        # Seed in-memory cache with 25 books
        catalog_cache.clear()
        for i in range(1, 26):
            add_or_update_catalog_book(
                book_id=i,
                clean_title=f"The Way of Kings Volume {i}",
                clean_author="Brandon Sanderson",
                formats=["AUDIO", "EPUB"]
            )
        catalog_cache._is_loaded = True

        bot_mock = MagicMock()
        bot_mock.get_me = AsyncMock(return_value=MagicMock(username="AudioSoulBot"))

        iq_mock = MagicMock()
        iq_mock.query = "Brandon Sanderson"
        iq_mock.answer = AsyncMock()

        with patch.object(settings, "BOT_USERNAME", "AudioSoulBot"):
            await handle_inline_query(iq_mock, bot_mock)

            iq_mock.answer.assert_called_once()
            results = iq_mock.answer.call_args[1]["results"]

            # Must return exactly 20 (capped at 20)
            self.assertEqual(len(results), 20)

            # Check article 1 structure and deep link
            first_article = results[0]
            self.assertIn("Brandon Sanderson", first_article.description)
            deep_link_btn = first_article.reply_markup.inline_keyboard[0][0]
            self.assertEqual(deep_link_btn.text, "📥 Get Files in Bot")
            self.assertTrue(deep_link_btn.url.startswith("https://t.me/AudioSoulBot?start=book_"))


if __name__ == "__main__":
    unittest.main()
