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


@router.message(IsAllowedGroupTopic(), GroupCommandFilter(["request"]))
async def handle_group_request(message: Message, query: str = "", bot: Optional[Bot] = None) -> None:
    """
    Handles /request <query>, #request <query>, or /request in reply to a message in groups.
    Saves the requested title to the database and confirms with zero group spam.
    """
    if bot is None:
        bot = getattr(message, "bot", None)
    bot_username = get_bot_username(bot)

    resolved_query = query.strip()

    # If query is empty, check if user replied to another message
    if not resolved_query and message.reply_to_message:
        reply_msg = message.reply_to_message
        reply_content = reply_msg.text or reply_msg.caption or ""
        if reply_content.strip():
            # Extract first line or clean snippet from the replied message
            first_line = reply_content.strip().split("\n")[0].strip()
            resolved_query = first_line[:200]

    # If still empty, display brief usage instruction
    if not resolved_query:
        usage_text = (
            "📋 <b>Community Request Desk</b>\n\n"
            "<b>Usage:</b>\n"
            "• <code>/request &lt;book title or author&gt;</code>\n"
            "• <code>#request &lt;book title or author&gt;</code>\n"
            "• Or reply to any message with <code>/request</code>"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💬 Submit in Bot DM", url=f"https://t.me/{bot_username}?start=request")]
            ]
        )
        await message.reply(
            usage_text,
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    # User registration & request tracking in DB
    user = message.from_user
    user_id = user.id if user else message.chat.id
    if user:
        await register_or_update_user(user.id, user.username)

    req_id = await create_request(user_id=user_id, query=resolved_query)

    confirmation_text = (
        f"✅ <b>Request Received!</b> (ID: <code>#{req_id}</code>)\n\n"
        f"📖 <i>'{resolved_query}'</i> has been submitted to curators.\n"
        "You will receive an automated notification in bot DM once it is added to the library!"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Open Bot DM", url=f"https://t.me/{bot_username}")]
        ]
    )

    await message.reply(
        confirmation_text,
        parse_mode="HTML",
        reply_markup=markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )
