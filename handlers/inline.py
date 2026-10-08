import logging
from typing import List
from aiogram import Router, Bot
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from config import settings
from database.models import search_books

logger = logging.getLogger(__name__)
router = Router(name="inline_router")


def format_inline_formats(formats: List[str]) -> str:
    """Formats formats list for inline query description, replacing ZIP/RAR/TAR with Audiobook."""
    badges = []
    has_audio = False
    for f in formats:
        f_up = f.upper()
        if f_up in ["AUDIO", "AUDIOBOOK", "ZIP", "RAR", "TAR"]:
            if not has_audio:
                badges.append("Audiobook")
                has_audio = True
        elif f_up not in badges:
            badges.append(f_up)
    return ", ".join(badges)


@router.inline_query()
async def handle_inline_query(inline_query: InlineQuery, bot: Bot) -> None:
    """Handles inline search query across all chats (@AudioSoulBot <query>)."""
    query = inline_query.query.strip()
    if not query:
        await inline_query.answer(
            results=[],
            switch_pm_text="🔍 Type book title or author to search...",
            switch_pm_parameter="search",
            cache_time=5,
            is_personal=True
        )
        return

    results, total = await search_books(query, limit=10, offset=0)
    if not results:
        await inline_query.answer(
            results=[],
            switch_pm_text="❌ No books found. Tap to open bot.",
            switch_pm_parameter="notfound",
            cache_time=5,
            is_personal=True
        )
        return

    bot_user = await bot.get_me()
    bot_username = settings.BOT_USERNAME or bot_user.username

    articles: List[InlineQueryResultArticle] = []
    for book in results:
        book_id = book["id"]
        title = book["clean_title"]
        author = book["clean_author"]
        formats = book.get("formats", [])
        formats_label = format_inline_formats(formats) if formats else "No files"

        deep_link = f"https://t.me/{bot_username}?start=book_{book_id}"

        # Card message when article is shared
        message_lines = [
            f"📖 <b>{title}</b>",
            f"✍️ <i>{author}</i>",
            f"📁 <b>Available Formats:</b> {formats_label}"
        ]
        if book.get("rating"):
            message_lines.append(f"⭐ <b>Rating:</b> {book['rating']}/5")
        if book.get("year"):
            message_lines.append(f"📅 <b>Year:</b> {book['year']}")

        if book.get("description"):
            desc = book["description"]
            short_desc = desc[:300].rstrip() + ("..." if len(desc) > 300 else "")
            message_lines.append(f"\n<i>{short_desc}</i>")

        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="📥 Get Files in Bot", url=deep_link)
            ]]
        )

        article = InlineQueryResultArticle(
            id=str(book_id),
            title=title,
            description=f"by {author} | Available: [{formats_label}]",
            thumbnail_url=book.get("cover_url"),
            input_message_content=InputTextMessageContent(
                message_text="\n".join(message_lines),
                parse_mode="HTML"
            ),
            reply_markup=reply_markup
        )
        articles.append(article)

    await inline_query.answer(
        results=articles,
        cache_time=10,
        is_personal=False
    )
