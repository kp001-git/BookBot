import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from config import settings
from database.models import get_stats

logger = logging.getLogger(__name__)
router = Router(name="admin_router")


@router.message(Command("stats"))
async def handle_admin_stats(message: Message) -> None:
    """Admin command: returns bot usage, library metrics, and format breakdown."""
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

    stats_text = (
        "📊 <b>Library & Bot Statistics</b>\n\n"
        f"📚 <b>Total Unique Books:</b> {total_books}\n\n"
        "📁 <b>Indexed Media Breakdown:</b>\n"
        f"  • 🎧 Audiobooks: {audio_count}\n"
        f"  • 📱 EPUBs: {epub_count}\n"
        f"  • 📄 PDFs: {pdf_count}\n"
        f"  • 🗜 ZIPs: {zip_count}\n\n"
        f"👥 <b>Total Registered Users:</b> {total_users}\n"
        f"📥 <b>Total Downloads Delivered:</b> {total_downloads}"
    )

    await message.answer(stats_text, parse_mode="HTML")
