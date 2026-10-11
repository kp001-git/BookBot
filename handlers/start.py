import logging
from typing import Optional, Union
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from config import settings
from database.models import (
    get_book_by_id,
    register_or_update_user
)
from middlewares.subscription import (
    check_user_subscription,
    build_subscription_markup,
    get_subscription_gate_message
)
from handlers.search import (
    render_book_card,
    ensure_book_cover,
    deliver_file_by_id,
    execute_and_send_search,
    _get_query
)

logger = logging.getLogger(__name__)
router = Router(name="start_router")

WELCOME_TEXT = (
    "📚 <b>Welcome to the Digital Library & Audiobook Indexer!</b>\n\n"
    "🔍 <b>How to Search:</b>\n"
    "• Send any title or author name directly in this chat.\n"
    "• Or use inline mode anywhere by typing <code>@AudioSoulBot title</code>.\n\n"
    "⚡ All files are delivered instantly in native formats (Audiobooks, EPUB, PDF)!\n\n"
    "👨‍💻 Developed by @souldumpp"
)


async def deliver_book_card_to_user(target: Message, book_id: int) -> None:
    """Delivers full book detail card with cover photo or text fallback."""
    book = await get_book_by_id(book_id)
    if not book:
        await target.answer("⚠️ Sorry, that book could not be found.")
        return

    await ensure_book_cover(book)
    card = render_book_card(book)

    if card.cover_url:
        try:
            await target.answer_photo(
                photo=card.cover_url,
                caption=card.caption_text,
                parse_mode="HTML",
                reply_markup=card.markup
            )
            return
        except Exception as img_err:
            logger.warning(f"Failed to send cover photo ({img_err}), falling back to text.")

    await target.answer(card.text, parse_mode="HTML", reply_markup=card.markup)


@router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject, bot: Optional[Bot] = None) -> None:
    """
    Handles /start and /start book_<id> deep links.
    Enforces channel subscription gatekeeper prior to delivering content.
    """
    if bot is None:
        bot = getattr(message, "bot", None)

    user = message.from_user
    if user:
        await register_or_update_user(user.id, user.username)

    args = command.args
    target_book_id: Optional[int] = None
    pending_payload = "start"

    if args and args.startswith("book_"):
        raw_id = args.split("book_")[1]
        if raw_id.isdigit():
            target_book_id = int(raw_id)
            pending_payload = f"book:{target_book_id}"

    # Force-Subscribe Gatekeeper Check (only if user and bot are present)
    if user and bot:
        user_id = user.id
        is_subscribed, unsubscribed = await check_user_subscription(bot, user_id)
        if not is_subscribed:
            gate_msg = get_subscription_gate_message(unsubscribed)
            markup = build_subscription_markup(pending_payload)
            await message.answer(
                gate_msg,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True
            )
            return

    # Deep link resolved for subscribed user
    if target_book_id is not None:
        await deliver_book_card_to_user(message, target_book_id)
        return

    # Standard Welcome Message
    await message.answer(WELCOME_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("verify_sub"))
async def handle_verify_subscription(callback: CallbackQuery, bot: Bot) -> None:
    """
    Handles '🔄 Verify Membership' callback query.
    Re-verifies membership across all required channels and immediately
    delivers the preserved target item upon successful validation.
    """
    user = callback.from_user
    is_subscribed, unsubscribed = await check_user_subscription(bot, user.id)

    if not is_subscribed:
        unsub_names = ", ".join(unsubscribed)
        await callback.answer(
            f"⚠️ You have not joined all required channels yet!\nPlease join: {unsub_names} and tap Verify again.",
            show_alert=True
        )
        return

    await callback.answer("✅ Membership verified! Loading your request...")

    # Extract pending action/payload preserved in callback data
    cb_data = callback.data or ""
    payload = cb_data.split("verify_sub:")[1] if "verify_sub:" in cb_data else "start"

    # Clean up gatekeeper prompt message
    target_msg = callback.message
    if target_msg:
        try:
            await target_msg.delete()
        except Exception:
            pass

    # Automatically deliver pending item
    if payload.startswith("book:"):
        raw_id = payload.split("book:")[1]
        if raw_id.isdigit() and target_msg:
            await deliver_book_card_to_user(target_msg, int(raw_id))
            return

    elif payload.startswith("dl:"):
        raw_id = payload.split("dl:")[1]
        if raw_id.isdigit() and target_msg:
            await deliver_file_by_id(bot, user.id, int(raw_id), target_msg)
            return

    elif payload.startswith("search:"):
        query_info = payload.split("search:")[1]
        query = _get_query(int(query_info)) if query_info.isdigit() else query_info
        if query and target_msg:
            await execute_and_send_search(target_msg, query)
            return

    # Default to welcome message
    if target_msg:
        await target_msg.answer(WELCOME_TEXT, parse_mode="HTML", disable_web_page_preview=True)
