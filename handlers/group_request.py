import re
import html
import logging
from typing import Optional
from aiogram import Router, Bot
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LinkPreviewOptions
)

from config import settings
from filters.group_topic import IsAllowedGroupTopic, GroupCommandFilter
from database.models import create_request, register_or_update_user

logger = logging.getLogger(__name__)
router = Router(name="group_request_router")


def get_bot_username(bot: Optional[Bot] = None) -> str:
    """Returns the bot username for generating deep link URLs."""
    if settings.BOT_USERNAME:
        return settings.BOT_USERNAME.lstrip("@")
    return "AudioSoulBot"


def clean_request_query(raw: str) -> str:
    """
    Cleans extracted query string:
    - Removes leading command triggers (e.g., /request, #request, /search, #search).
    - Takes first non-empty line if multi-line.
    - Strips leading/trailing hashtags, quotes, and punctuation.
    - Truncates to reasonable length (200 chars).
    """
    if not raw:
        return ""
    text = raw.strip()
    text = re.sub(r"^(?:/|#)(?:request|search)(?:@\w+)?\s*", "", text, flags=re.IGNORECASE)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if lines:
        text = lines[0]
    text = text.strip("# \t\r\n\"'.,;:!?-")
    return text[:200]


@router.message(IsAllowedGroupTopic(), GroupCommandFilter(["request"]))
async def handle_group_request(message: Message, query: str = "", bot: Optional[Bot] = None) -> None:
    """
    Handles /request <query> and #request <query> in groups and allowed forum topics.
    Supports Admin Proxy Request on Reply:
    - If replying to another user's message, attributes request to that user and replies to their message.
    - Otherwise, handles user's self-request and replies to their own message.
    """
    if bot is None:
        bot = getattr(message, "bot", None)
    bot_username = get_bot_username(bot)

    # 1. Detect Proxy vs Direct Request
    reply_msg = message.reply_to_message
    if reply_msg and reply_msg.from_user:
        target_user = reply_msg.from_user
        target_msg = reply_msg
        if query and query.strip():
            raw_query = query.strip()
        else:
            raw_query = reply_msg.text or reply_msg.caption or ""
    else:
        target_user = message.from_user
        target_msg = message
        raw_query = query.strip()

    # 2. Clean Query Extraction
    clean_query = clean_request_query(raw_query)

    if not clean_query:
        await message.reply(
            "Please provide a book title or reply to a message containing one.",
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    # 3. Database Insertion & Notification
    target_user_id = target_user.id if target_user else message.chat.id
    target_name = (
        getattr(target_user, "full_name", None)
        or getattr(target_user, "first_name", None)
        or "Reader"
    )

    if target_user:
        await register_or_update_user(target_user.id, target_user.username)

    await create_request(user_id=target_user_id, query=clean_query)

    escaped_name = html.escape(str(target_name))
    escaped_query = html.escape(clean_query)

    notification_text = (
        f'Hey <a href="tg://user?id={target_user_id}">{escaped_name}</a>! 📚✨\n'
        f'Your request for "<b>{escaped_query}</b>" has been registered into our library curation queue.'
    )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Open Bot DM", url=f"https://t.me/{bot_username}")]
        ]
    )

    await target_msg.reply(
        notification_text,
        parse_mode="HTML",
        reply_markup=markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )
