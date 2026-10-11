import logging
from typing import Optional
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from database.models import (
    get_book_by_id,
    update_book_metadata,
    delete_book_file,
    get_user_role
)
from services.search import add_or_update_catalog_book

logger = logging.getLogger(__name__)
router = Router(name="editor_router")


class EditorStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_author = State()


async def _is_authorized(user_id: int) -> bool:
    """Checks whether the user has admin or moderator permissions."""
    role = await get_user_role(user_id)
    return role in ("admin", "moderator")


def _build_editor_markup(book: dict) -> InlineKeyboardMarkup:
    """Builds inline management control buttons for the catalog editor panel."""
    book_id = book["id"]
    rows = [
        [
            InlineKeyboardButton(text="✏️ Edit Title", callback_data=f"edit_act:title:{book_id}"),
            InlineKeyboardButton(text="✍️ Edit Author", callback_data=f"edit_act:author:{book_id}")
        ]
    ]

    files = book.get("files") or []
    for f in files:
        f_id = f.get("id")
        fmt = f.get("format", "FILE")
        name = f.get("file_name") or f"File #{f_id}"
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 Delete [{fmt}] {name[:24]}",
                callback_data=f"edit_act:delfile:{book_id}:{f_id}"
            )
        ])

    rows.append([InlineKeyboardButton(text="❌ Close Panel", callback_data="edit_act:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_editor_panel(book: dict) -> str:
    """Renders the text view for catalog editor."""
    files = book.get("files") or []
    files_str = "\n".join(
        [f"  • [ID: {f.get('id')}] <b>{f.get('format')}</b>: {f.get('file_name') or 'Unnamed'}" for f in files]
    ) or "  • <i>No files linked</i>"

    return (
        f"🛠 <b>Catalog Editor — Book #{book['id']}</b>\n\n"
        f"📖 <b>Title:</b> {book.get('clean_title')}\n"
        f"✍️ <b>Author:</b> {book.get('clean_author')}\n"
        f"⭐ <b>Rating:</b> {book.get('rating') or 'N/A'}\n"
        f"🏷 <b>Genres:</b> {book.get('genres') or 'N/A'}\n\n"
        f"📁 <b>Linked Media Files:</b>\n{files_str}\n\n"
        f"<i>Select an action below to update metadata or delete corrupted files:</i>"
    )


@router.message(Command("edit"))
async def handle_edit_command(message: Message, state: FSMContext) -> None:
    """
    /edit <book_id> - In-chat catalog curation panel.
    Restricted to admin and moderator roles.
    """
    user = message.from_user
    if not user or not await _is_authorized(user.id):
        await message.answer("⛔ Access denied. Catalog editing is restricted to administrators and moderators.")
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("ℹ️ <b>Usage:</b> <code>/edit &lt;book_id&gt;</code>\nExample: <code>/edit 42</code>", parse_mode="HTML")
        return

    book_id = int(parts[1])
    book = await get_book_by_id(book_id)
    if not book:
        await message.answer(f"⚠️ Book #{book_id} could not be found.")
        return

    await state.clear()
    panel_text = _format_editor_panel(book)
    markup = _build_editor_markup(book)
    await message.answer(panel_text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.startswith("edit_act:"))
async def handle_editor_action(callback: CallbackQuery, state: FSMContext) -> None:
    """Handles editor panel button callbacks."""
    user = callback.from_user
    if not await _is_authorized(user.id):
        await callback.answer("⛔ Unauthorized action.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]

    if action == "close":
        await state.clear()
        if callback.message:
            try:
                await callback.message.delete()
            except Exception:
                pass
        await callback.answer("Panel closed.")
        return

    if action == "title":
        book_id = int(parts[2])
        await state.set_state(EditorStates.waiting_for_title)
        await state.update_data(book_id=book_id, original_msg_id=callback.message.message_id if callback.message else None)
        await callback.answer()
        await callback.message.reply(f"✏️ <b>Send the new Title for Book #{book_id}:</b>", parse_mode="HTML")
        return

    if action == "author":
        book_id = int(parts[2])
        await state.set_state(EditorStates.waiting_for_author)
        await state.update_data(book_id=book_id, original_msg_id=callback.message.message_id if callback.message else None)
        await callback.answer()
        await callback.message.reply(f"✍️ <b>Send the new Author name for Book #{book_id}:</b>", parse_mode="HTML")
        return

    if action == "delfile":
        book_id = int(parts[2])
        file_id = int(parts[3])
        success = await delete_book_file(file_id)
        if success:
            await callback.answer(f"✅ File #{file_id} deleted successfully.")
            # Refresh book panel
            updated_book = await get_book_by_id(book_id)
            if updated_book and callback.message:
                formats = list({f["format"] for f in updated_book.get("files", []) if f.get("format")})
                add_or_update_catalog_book(
                    book_id=book_id,
                    clean_title=updated_book["clean_title"],
                    clean_author=updated_book.get("clean_author"),
                    formats=formats,
                    cover_url=updated_book.get("cover_url"),
                    description=updated_book.get("description")
                )
                panel_text = _format_editor_panel(updated_book)
                markup = _build_editor_markup(updated_book)
                try:
                    await callback.message.edit_text(panel_text, parse_mode="HTML", reply_markup=markup)
                except Exception:
                    pass
        else:
            await callback.answer(f"❌ Could not delete file #{file_id}.", show_alert=True)


@router.message(EditorStates.waiting_for_title)
async def handle_new_title(message: Message, state: FSMContext) -> None:
    """FSM handler for applying updated title."""
    user = message.from_user
    if not user or not await _is_authorized(user.id):
        await state.clear()
        return

    new_title = (message.text or "").strip()
    if not new_title:
        await message.answer("⚠️ Title cannot be empty. Please send a valid title:")
        return

    data = await state.get_data()
    book_id = data.get("book_id")
    await state.clear()

    if not book_id:
        await message.answer("⚠️ Session expired. Please run /edit again.")
        return

    await update_book_metadata(book_id=book_id, title=new_title)
    updated_book = await get_book_by_id(book_id)
    if updated_book:
        formats = list({f["format"] for f in updated_book.get("files", []) if f.get("format")})
        add_or_update_catalog_book(
            book_id=book_id,
            clean_title=updated_book["clean_title"],
            clean_author=updated_book.get("clean_author"),
            formats=formats,
            cover_url=updated_book.get("cover_url"),
            description=updated_book.get("description")
        )

    await message.answer(f"✅ <b>Title updated!</b>\nNew Title: <b>{new_title}</b>", parse_mode="HTML")


@router.message(EditorStates.waiting_for_author)
async def handle_new_author(message: Message, state: FSMContext) -> None:
    """FSM handler for applying updated author."""
    user = message.from_user
    if not user or not await _is_authorized(user.id):
        await state.clear()
        return

    new_author = (message.text or "").strip()
    if not new_author:
        await message.answer("⚠️ Author cannot be empty. Please send a valid author name:")
        return

    data = await state.get_data()
    book_id = data.get("book_id")
    await state.clear()

    if not book_id:
        await message.answer("⚠️ Session expired. Please run /edit again.")
        return

    await update_book_metadata(book_id=book_id, author=new_author)
    updated_book = await get_book_by_id(book_id)
    if updated_book:
        formats = list({f["format"] for f in updated_book.get("files", []) if f.get("format")})
        add_or_update_catalog_book(
            book_id=book_id,
            clean_title=updated_book["clean_title"],
            clean_author=updated_book.get("clean_author"),
            formats=formats,
            cover_url=updated_book.get("cover_url"),
            description=updated_book.get("description")
        )

    await message.answer(f"✅ <b>Author updated!</b>\nNew Author: <b>{new_author}</b>", parse_mode="HTML")
