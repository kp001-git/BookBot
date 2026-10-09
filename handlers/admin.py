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
