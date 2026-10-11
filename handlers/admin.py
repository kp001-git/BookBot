import time
import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message

from config import settings
from database.connection import get_db_engine_name, is_postgres
from database.models import get_stats
from database.backup import send_database_backup

logger = logging.getLogger(__name__)
router = Router(name="admin_router")

_START_TIME = time.time()


def get_uptime_duration() -> str:
    """Computes human-readable bot uptime."""
    elapsed = int(time.time() - _START_TIME)
    days, remainder = divmod(elapsed, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


@router.message(Command("stats"))
async def handle_admin_stats(message: Message) -> None:
    """Admin command: returns bot usage, library metrics, format breakdown, db engine, and uptime."""
    user = message.from_user
    if not user or user.id not in settings.ADMIN_IDS:
        logger.warning(f"Unauthorized /stats attempt by user {user.id if user else 'Unknown'}")
        await message.answer("⛔ Access denied. This command is restricted to administrators.")
        return

    stats = await get_stats()
    total_books = stats["total_books"]
    format_counts = stats["format_counts"]
    total_users = stats["total_users"]
    total_downloads = stats["total_downloads"]

    audio_count = format_counts.get("AUDIO", 0)
    epub_count = format_counts.get("EPUB", 0)
    pdf_count = format_counts.get("PDF", 0)
    zip_count = format_counts.get("ZIP", 0)

    db_engine = get_db_engine_name()
    uptime = get_uptime_duration()

    stats_text = (
        "📊 <b>Library & Bot Statistics</b>\n\n"
        f"📚 <b>Total Indexed Books:</b> {total_books}\n\n"
        "📁 <b>Indexed Media Breakdown:</b>\n"
        f"  • 🎧 Audiobooks: {audio_count}\n"
        f"  • 📱 EPUBs: {epub_count}\n"
        f"  • 📄 PDFs: {pdf_count}\n"
        f"  • 🗜 ZIPs: {zip_count}\n\n"
        f"👥 <b>Total Unique Users:</b> {total_users}\n"
        f"📥 <b>Total File Deliveries Served:</b> {total_downloads}\n\n"
        f"💾 <b>Current Database Engine:</b> {db_engine}\n"
        f"⏱ <b>Uptime Duration:</b> {uptime}"
    )

    await message.answer(stats_text, parse_mode="HTML")


@router.message(Command("backup"))
async def handle_admin_backup(message: Message, bot: Bot) -> None:
    """Admin command: triggers instant SQLite snapshot sent to the requesting admin."""
    user = message.from_user
    if not user or user.id not in settings.ADMIN_IDS:
        logger.warning(f"Unauthorized /backup attempt by user {user.id if user else 'Unknown'}")
        await message.answer("⛔ Access denied. This command is restricted to administrators.")
        return

    if is_postgres():
        await message.answer(
            "ℹ️ <b>Active Database:</b> PostgreSQL\n\n"
            "Cloud database persistence is active via managed PostgreSQL.",
            parse_mode="HTML"
        )
        return

    status_msg = await message.answer("⏳ Generating database snapshot...")
    try:
        await send_database_backup(bot, target_chat_id=message.chat.id)
        try:
            await status_msg.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Failed to generate backup: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Failed to generate database snapshot: {e}")


@router.message(Command("broadcast"))
async def handle_admin_broadcast(message: Message, bot: Bot) -> None:
    """
    Admin command: safe broadcast to all registered users.
    Rate limited to 25 messages/second to prevent Telegram flood limits.
    """
    user = message.from_user
    if not user or user.id not in settings.ADMIN_IDS:
        await message.answer("⛔ Access denied. Broadcasting is restricted to administrators.")
        return

    text = message.text or ""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "📢 <b>Broadcast Console</b>\n\n"
            "<b>Usage:</b>\n"
            "<code>/broadcast &lt;HTML or Text Message&gt;</code>\n\n"
            "<i>Example:</i> <code>/broadcast 📚 Over 100 new audiobooks have been added! Check them out with /search</code>",
            parse_mode="HTML"
        )
        return

    broadcast_content = parts[1].strip()
    from database.models import get_all_user_ids
    user_ids = await get_all_user_ids()

    if not user_ids:
        await message.answer("⚠️ No registered users found in the database to broadcast to.")
        return

    progress_msg = await message.answer(
        f"⏳ <b>Starting Broadcast...</b>\nTarget recipients: {len(user_ids)} users\nRate: 25 msg/sec",
        parse_mode="HTML"
    )

    import asyncio
    from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

    sent_count = 0
    blocked_count = 0
    failed_count = 0

    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=broadcast_content, parse_mode="HTML")
            sent_count += 1
        except TelegramForbiddenError:
            blocked_count += 1
        except TelegramRetryAfter as retry:
            await asyncio.sleep(retry.retry_after)
            try:
                await bot.send_message(chat_id=uid, text=broadcast_content, parse_mode="HTML")
                sent_count += 1
            except Exception:
                failed_count += 1
        except Exception as err:
            logger.debug(f"Broadcast delivery failed for user {uid}: {err}")
            failed_count += 1

        # Strict rate limit: 25 msgs/sec -> 1/25 = 0.04s delay per message
        await asyncio.sleep(0.04)

    summary_text = (
        "📢 <b>Broadcast Completed!</b>\n\n"
        f"👥 <b>Total Targets:</b> {len(user_ids)}\n"
        f"✅ <b>Successfully Sent:</b> {sent_count}\n"
        f"🚫 <b>Blocked / Deactivated:</b> {blocked_count}\n"
        f"❌ <b>Other Failures:</b> {failed_count}"
    )

    try:
        await progress_msg.edit_text(summary_text, parse_mode="HTML")
    except Exception:
        await message.answer(summary_text, parse_mode="HTML")

