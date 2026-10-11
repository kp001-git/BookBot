import logging
from typing import Optional
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database.models import (
    toggle_favorite,
    is_favorite,
    get_user_favorites,
    get_book_by_id
)
from handlers.search import render_book_card

logger = logging.getLogger(__name__)
router = Router(name="favorites_router")

FAVS_PER_PAGE = 5


@router.callback_query(F.data.startswith("fav:"))
async def handle_toggle_favorite(callback: CallbackQuery) -> None:
    """
    Handles clicking '⭐ Favorite' / '❌ Remove Favorite' button.
    Toggles the book's bookmark state for the user and dynamically updates the button.
    Data format: fav:{book_id}:{query_id}:{page}
    """
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("Invalid request.", show_alert=True)
        return

    try:
        book_id = int(parts[1])
        query_id = int(parts[2])
        page = int(parts[3])
    except ValueError:
        await callback.answer("Invalid parameters.", show_alert=True)
        return

    user_id = callback.from_user.id
    new_state = await toggle_favorite(user_id, book_id)

    # Re-render card markup with the updated favorite status
    book = await get_book_by_id(book_id)
    if book and callback.message:
        card = render_book_card(book, query_id=query_id, page=page, is_fav=new_state)
        try:
            await callback.message.edit_reply_markup(reply_markup=card.markup)
        except Exception as e:
            logger.debug(f"Failed to edit markup after favorite toggle: {e}")

    alert_text = "⭐ Added to your favorites!" if new_state else "❌ Removed from your favorites."
    await callback.answer(alert_text)


def _build_favorites_markup(favorites: list[dict], page: int = 1) -> InlineKeyboardMarkup:
    total = len(favorites)
    total_pages = max(1, (total + FAVS_PER_PAGE - 1) // FAVS_PER_PAGE)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * FAVS_PER_PAGE
    page_favs = favorites[start_idx:start_idx + FAVS_PER_PAGE]

    rows = []
    for fav in page_favs:
        title = fav.get("title") or "Unknown Title"
        author = fav.get("author") or "Unknown Author"
        b_id = fav.get("book_id")
        btn_text = f"📖 {title[:32]} - {author[:18]}"
        rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"book:{b_id}:0:1")])

    nav_row = []
    if current_page > 1:
        nav_row.append(InlineKeyboardButton(text="◀️ Prev", callback_data=f"fav_page:{current_page - 1}"))
    if total_pages > 1:
        nav_row.append(InlineKeyboardButton(text=f"📄 {current_page}/{total_pages}", callback_data="noop"))
    if current_page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"fav_page:{current_page + 1}"))

    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("favorites"))
async def handle_favorites_command(message: Message) -> None:
    """Lists books bookmarked by the user."""
    user = message.from_user
    if not user:
        return

    favs = await get_user_favorites(user.id)
    if not favs:
        await message.answer(
            "⭐ <b>Your Library Bookmarks</b>\n\n"
            "You haven't added any books to your favorites yet!\n"
            "Search for a book and tap <b>⭐ Favorite</b> to save it here for quick access.",
            parse_mode="HTML"
        )
        return

    markup = _build_favorites_markup(favs, page=1)
    await message.answer(
        f"⭐ <b>Your Library Bookmarks</b> ({len(favs)} saved)\n\n"
        "Tap any title below to view book details & download files:",
        parse_mode="HTML",
        reply_markup=markup
    )


@router.callback_query(F.data.startswith("fav_page:"))
async def handle_favorites_page_callback(callback: CallbackQuery) -> None:
    """Handles pagination for favorites list."""
    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        page = 1

    user_id = callback.from_user.id
    favs = await get_user_favorites(user_id)
    if not favs:
        await callback.answer("No favorites found.")
        return

    markup = _build_favorites_markup(favs, page=page)
    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except Exception:
        pass
    await callback.answer()
