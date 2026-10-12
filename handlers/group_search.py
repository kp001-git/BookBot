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
from services.search import search_catalog

logger = logging.getLogger(__name__)
router = Router(name="group_search_router")


def get_bot_username(bot: Optional[Bot] = None) -> str:
    """Returns the bot username for generating deep link URLs."""
    if settings.BOT_USERNAME:
        return settings.BOT_USERNAME.lstrip("@")
    return "AudioSoulBot"


@router.message(IsAllowedGroupTopic(), GroupCommandFilter(["search"]))
async def handle_group_search(message: Message, query: str = "", bot: Optional[Bot] = None) -> None:
    """
    Handles /search <query> and #search <query> in groups and allowed forum topics.
    Performs fast in-memory search and replies with a zero-spam compact card
    with deep link buttons redirecting the user into bot private chat.
    """
    if bot is None:
        bot = getattr(message, "bot", None)
    bot_username = get_bot_username(bot)

    if not query:
        usage_text = (
            "ℹ️ <b>How to Search:</b>\n"
            "Send <code>/search &lt;book title or author&gt;</code> or <code>#search &lt;title&gt;</code>\n"
            "<i>Example:</i> <code>/search Dune</code>"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Search in Bot DM", url=f"https://t.me/{bot_username}")]
            ]
        )
        await message.reply(
            usage_text,
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    # Execute search against in-memory catalog cache (<2ms)
    results, total = await search_catalog(query, limit=5, offset=0)

    if total == 0 or not results:
        not_found_text = (
            f"🔍 <b>Book not found in library for '{query}'.</b>\n"
            "Use <code>/request</code> to submit it to curators."
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💬 Request in Bot DM", url=f"https://t.me/{bot_username}?start=request")]
            ]
        )
        await message.reply(
            not_found_text,
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    # Results found: Build compact summary card and deep link buttons
    display_results = results[:5]
    lines = [
        f"📚 <b>Found {total} result(s) for</b> <i>'{query}'</i>:\n"
    ]
    buttons = []

    for idx, book in enumerate(display_results, 1):
        title = book.get("clean_title") or "Unknown Title"
        author = book.get("clean_author") or "Unknown Author"
        fmts = book.get("formats", [])
        badge = f" [{', '.join(fmts)}]" if fmts else ""
        lines.append(f"{idx}. <b>{title}</b> — <i>{author}</i>{badge}")

        # Inline button text limit is 64 characters
        # "📥 Get " (7 chars) + title + " in Bot" (7 chars) = 14 overhead
        max_title_len = 64 - 14
        short_title = title if len(title) <= max_title_len else title[:max_title_len - 3].rstrip() + "..."
        btn_text = f"📥 Get {short_title} in Bot"
        deep_link = f"https://t.me/{bot_username}?start=book_{book['id']}"
        buttons.append([InlineKeyboardButton(text=btn_text, url=deep_link)])

    lines.append("\n👉 <i>Tap a button below to get book files in private chat:</i>")
    summary_text = "\n".join(lines)
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.reply(
        summary_text,
        parse_mode="HTML",
        reply_markup=markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )
