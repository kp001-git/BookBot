import logging
from typing import Optional
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LinkPreviewOptions
)

from database.models import (
    create_request,
    get_requests,
    update_request_status,
    get_user_role
)

logger = logging.getLogger(__name__)
router = Router(name="request_router")


async def _is_staff(user_id: int) -> bool:
    """Checks whether user is an admin or moderator."""
    role = await get_user_role(user_id)
    return role in ("admin", "moderator")


@router.message(F.chat.type == "private", Command("request"))
async def handle_request_command(message: Message) -> None:
    """
    /request <title or author> - Submits a book request to the community request desk.
    """
    user = message.from_user
    if not user:
        return

    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "📋 <b>Community Request Desk</b>\n\n"
            "Can't find a book in the library? Submit a request to our curating team!\n\n"
            "<b>Usage:</b>\n"
            "<code>/request &lt;book title or author&gt;</code>\n\n"
            "<i>Example:</i> <code>/request Atomic Habits by James Clear</code>",
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
        return

    query = parts[1].strip()
    req_id = await create_request(user.id, query)

    await message.answer(
        f"✅ <b>Request Submitted!</b> (ID: <code>#{req_id}</code>)\n\n"
        f"📖 <b>Query:</b> <i>{query}</i>\n\n"
        "Our curators have been notified. You will receive an instant notification here once it is added to the library!",
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )


def _build_request_admin_markup(requests_list: list[dict]) -> InlineKeyboardMarkup:
    """Builds inline action buttons for reviewing pending requests."""
    rows = []
    for req in requests_list:
        r_id = req["id"]
        q = req["query"][:25]
        # First row: title info button (no-op)
        rows.append([InlineKeyboardButton(text=f"📋 #{r_id}: {q}", callback_data=f"req_info:{r_id}")])
        # Second row: action buttons
        rows.append([
            InlineKeyboardButton(text="✅ Fulfilled", callback_data=f"req_act:fulfill:{r_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"req_act:reject:{r_id}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("requests"))
async def handle_requests_list_command(message: Message) -> None:
    """
    /requests - Admin & Moderator panel to inspect pending community requests.
    """
    user = message.from_user
    if not user or not await _is_staff(user.id):
        await message.answer("⛔ Access denied. This command is restricted to administrators and moderators.")
        return

    pending = await get_requests(status="pending", limit=10)
    if not pending:
        await message.answer("✨ <b>Community Request Desk</b>\n\nNo pending book requests at this time!", parse_mode="HTML")
        return

    text = (
        f"📋 <b>Pending Community Requests</b> ({len(pending)} open)\n\n"
        "Tap <b>Fulfilled</b> when indexed to automatically notify the requester, or <b>Reject</b>:"
    )
    markup = _build_request_admin_markup(pending)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.startswith("req_act:"))
async def handle_request_action(callback: CallbackQuery, bot: Bot) -> None:
    """Handles staff fulfilling or rejecting a book request."""
    user = callback.from_user
    if not await _is_staff(user.id):
        await callback.answer("⛔ Unauthorized action.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]
    req_id = int(parts[2])

    if action == "fulfill":
        req = await update_request_status(req_id, "fulfilled")
        if req:
            # Send automated DM to original requester
            orig_user_id = req["user_id"]
            query_text = req["query"]
            try:
                await bot.send_message(
                    chat_id=orig_user_id,
                    text=(
                        f"🎉 <b>Good news!</b> Your requested title <i>'{query_text}'</i> "
                        "is now available in the library!\n\n"
                        f"Send <code>{query_text}</code> directly to the bot to download."
                    ),
                    parse_mode="HTML"
                )
            except Exception as notify_err:
                logger.warning(f"Could not send DM to user {orig_user_id}: {notify_err}")

            await callback.answer(f"✅ Request #{req_id} marked fulfilled and user notified!")
        else:
            await callback.answer("⚠️ Request not found.", show_alert=True)

    elif action == "reject":
        await update_request_status(req_id, "rejected")
        await callback.answer(f"❌ Request #{req_id} marked rejected.")

    # Refresh the pending list
    pending = await get_requests(status="pending", limit=10)
    if not pending:
        if callback.message:
            try:
                await callback.message.edit_text("✨ <b>Community Request Desk</b>\n\nAll requests have been addressed!", parse_mode="HTML")
            except Exception:
                pass
    else:
        markup = _build_request_admin_markup(pending)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=markup)
            except Exception:
                pass


@router.callback_query(F.data.startswith("req_info:"))
async def handle_request_info(callback: CallbackQuery) -> None:
    """Shows full query text for a request upon clicking."""
    req_id = int(callback.data.split(":")[1])
    requests = await get_requests(status=None, limit=50)
    found = next((r for r in requests if r["id"] == req_id), None)
    if found:
        await callback.answer(f"Request #{req_id}:\nQuery: {found['query']}\nUser: {found['user_id']}", show_alert=True)
    else:
        await callback.answer(f"Request #{req_id}", show_alert=False)
