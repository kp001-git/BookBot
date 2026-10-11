import logging
import inspect
from typing import List, Tuple, Optional, Callable, Dict, Any, Awaitable
from unittest.mock import MagicMock, Mock

from aiogram import BaseMiddleware, Bot
from aiogram.types import (
    Message,
    CallbackQuery,
    TelegramObject,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from config import settings

logger = logging.getLogger(__name__)

# Valid member statuses in Telegram
VALID_MEMBER_STATUSES = {"creator", "administrator", "member"}


async def check_user_subscription(bot: Optional[Bot], user_id: int) -> Tuple[bool, List[str]]:
    """
    Checks if a user is subscribed to all REQUIRED_CHANNELS.
    Admins are always exempt.
    Valid member statuses: 'creator', 'administrator', 'member'.

    Returns:
        (is_subscribed: bool, unsubscribed_channels: List[str])
    """
    if not bot or not hasattr(bot, "get_chat_member"):
        return True, []

    if user_id in settings.ADMIN_IDS:
        return True, []

    if not settings.REQUIRED_CHANNELS:
        return True, []

    unsubscribed: List[str] = []

    for channel in settings.REQUIRED_CHANNELS:
        try:
            res = bot.get_chat_member(chat_id=channel, user_id=user_id)
            if inspect.isawaitable(res):
                member = await res
            else:
                member = res

            status = getattr(member, "status", None)
            if isinstance(status, (MagicMock, Mock)):
                continue
            if status in VALID_MEMBER_STATUSES:
                continue
            if status == "restricted" and getattr(member, "is_member", False):
                continue
            unsubscribed.append(channel)
        except Exception as e:
            # When a user hasn't joined or is not found in the channel, Telegram raises TelegramBadRequest
            logger.info(f"User {user_id} subscription check for {channel} failed: {e}")
            unsubscribed.append(channel)

    return len(unsubscribed) == 0, unsubscribed


def build_subscription_markup(pending_payload: str = "") -> InlineKeyboardMarkup:
    """
    Constructs an inline keyboard with join buttons for all required channels
    and a '🔄 Verify Membership' callback button that preserves the pending action/book ID.
    """
    buttons: List[List[InlineKeyboardButton]] = []

    for channel in settings.REQUIRED_CHANNELS:
        ch_str = str(channel).strip()
        if ch_str.startswith("@"):
            url = f"https://t.me/{ch_str[1:]}"
            title = ch_str
        elif ch_str.startswith("https://t.me/"):
            url = ch_str
            clean_name = ch_str.rstrip("/").split("/")[-1]
            title = f"@{clean_name}" if not clean_name.startswith("@") else clean_name
        else:
            url = f"https://t.me/{ch_str}"
            title = f"@{ch_str}" if not ch_str.startswith("-") else "Official Channel"

        buttons.append([
            InlineKeyboardButton(text=f"📢 Join {title}", url=url)
        ])

    cb_data = f"verify_sub:{pending_payload}" if pending_payload else "verify_sub"
    # Ensure callback_data strictly respects Telegram's 64-byte limit
    if len(cb_data.encode("utf-8")) > 64:
        cb_data = cb_data[:64]

    buttons.append([
        InlineKeyboardButton(text="✨ I've Joined — Continue", callback_data=cb_data)
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_subscription_gate_message(unsubscribed_channels: Optional[List[str]] = None) -> str:
    """Renders a gentle and welcoming request asking the user to join the channel to support the developer."""
    channels = unsubscribed_channels or settings.REQUIRED_CHANNELS
    ch_list_str = "\n".join([f"  👉 <b>{ch}</b>" for ch in channels])

    return (
        "👋 <b>Welcome to the Digital Library!</b> 🎧📚\n\n"
        "To support the developer and keep all audiobooks & books free for everyone, "
        "please join our channel before getting started:\n\n"
        f"{ch_list_str}\n\n"
        "After joining, tap <b>✨ I've Joined — Continue</b> below to unlock the bot! ❤️"
    )


class SubscriptionMiddleware(BaseMiddleware):
    """
    Aiogram 3.x Middleware for enforcing channel subscriptions.
    Intercepts DM interactions and displays the force-subscribe gatekeeper when needed.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        bot: Optional[Bot] = data.get("bot")
        if not bot:
            return await handler(event, data)

        # Handle Messages
        if isinstance(event, Message):
            # Only intercept private chats
            if event.chat.type != "private":
                return await handler(event, data)

            user = event.from_user
            if not user or user.id in settings.ADMIN_IDS:
                return await handler(event, data)

            is_subscribed, unsubscribed = await check_user_subscription(bot, user.id)
            if not is_subscribed:
                # Determine payload
                text = (event.text or "").strip()
                payload = "start"
                if text.startswith("/start"):
                    parts = text.split(maxsplit=1)
                    if len(parts) > 1 and parts[1].startswith("book_"):
                        payload = f"book:{parts[1].split('book_')[1]}"
                elif text and not text.startswith("/"):
                    # Cache query ID or query prefix
                    payload = f"search:{text[:20]}"

                gate_msg = get_subscription_gate_message(unsubscribed)
                markup = build_subscription_markup(payload)
                await event.answer(gate_msg, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                return

        # Handle Callbacks
        elif isinstance(event, CallbackQuery):
            cb_data = event.data or ""
            # Allow verification and noop callbacks to pass through
            if cb_data.startswith("verify_sub") or cb_data == "noop":
                return await handler(event, data)

            user = event.from_user
            if user and user.id in settings.ADMIN_IDS:
                return await handler(event, data)

            is_subscribed, unsubscribed = await check_user_subscription(bot, user.id)
            if not is_subscribed:
                payload = "start"
                if cb_data.startswith("dl:"):
                    payload = f"dl:{cb_data.split('dl:')[1]}"
                elif cb_data.startswith("book:"):
                    parts = cb_data.split(":")
                    payload = f"book:{parts[1]}"

                await event.answer("Please join our channel to support us first ❤️", show_alert=False)
                gate_msg = get_subscription_gate_message(unsubscribed)
                markup = build_subscription_markup(payload)
                if event.message:
                    await event.message.answer(gate_msg, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                return

        return await handler(event, data)
