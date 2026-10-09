import math
import logging
from typing import Dict, Any, List, Optional
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from config import config, settings
from utils.enricher import format_size, format_duration, enrich_book_metadata
from database.models import (
    search_books,
    get_book_by_id,
    get_file_by_id,
    update_book_metadata,
    register_or_update_user,
    increment_user_downloads
)

logger = logging.getLogger(__name__)
router = Router(name="search_router")

PAGE_SIZE = 5

# Memory cache for active search queries to guarantee callback_data stays well under Telegram's 64-byte limit
_QUERY_CACHE: Dict[int, str] = {}
_NEXT_QUERY_ID = 1


def _store_query(query: str) -> int:
    global _NEXT_QUERY_ID
    qid = _NEXT_QUERY_ID
    _QUERY_CACHE[qid] = query
    _NEXT_QUERY_ID += 1
    # Maintain cache size
    if len(_QUERY_CACHE) > 5000:
        oldest = min(_QUERY_CACHE.keys())
        _QUERY_CACHE.pop(oldest, None)
    return qid


def _get_query(qid: int) -> str:
    return _QUERY_CACHE.get(qid, "")


def is_archive_file(file_name: Optional[str], fmt: Optional[str] = None) -> bool:
    """Checks if a file is an archive packaging multi-track audiobooks."""
    name = (file_name or "").lower()
    format_upper = (fmt or "").upper()
    archive_exts = [".zip", ".rar", ".tar", ".7z", ".gz"]
    return any(name.endswith(ext) for ext in archive_exts) or format_upper in ["ZIP", "RAR", "TAR"]


def format_button_label(file_info: Dict[str, Any]) -> str:
    """Renders user-friendly button labels, reclassifying archives as Audiobooks."""
    fmt = (file_info.get("format") or "").upper()
    fname = (file_info.get("file_name") or "").lower()
    size = format_size(file_info.get("file_size"))
    size_str = f" ({size})" if size != "N/A" else ""

    if is_archive_file(fname, fmt):
        # Extract archive type (ZIP, RAR, etc.)
        archive_type = "ZIP"
        for ext in [".rar", ".tar", ".7z", ".gz", ".zip"]:
            if fname.endswith(ext):
                archive_type = ext[1:].upper()
                break
        return f"🎧 Audiobook ({archive_type}){size_str}"

    if fmt in ["AUDIO", "AUDIOBOOK"]:
        audio_ext = ""
        for ext in [".m4b", ".mp3", ".m4a", ".flac", ".opus"]:
            if fname.endswith(ext):
                audio_ext = f" ({ext})"
                break
        return f"🎧 Audiobook{audio_ext}{size_str}"
    elif fmt == "EPUB":
        return f"📱 EPUB{size_str}"
    elif fmt == "PDF":
        return f"📄 PDF{size_str}"
    else:
        return f"📁 {fmt}{size_str}"


def format_display_badges(formats: List[str]) -> str:
    """Formats formats list for UI badges, replacing ZIP/RAR/TAR with Audiobook."""
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


class BookCardPayload:
    def __init__(self, text: str, caption_text: str, markup: InlineKeyboardMarkup, cover_url: Optional[str] = None):
        self.text = text
        self.caption_text = caption_text
        self.markup = markup
        self.cover_url = cover_url


def render_book_card(book: Dict[str, Any], query_id: int = 0, page: int = 1) -> BookCardPayload:
    """Renders full book card and caption-sized version with format buttons."""
    header_lines = [
        f"📖 <b>Title:</b> {book['clean_title']}",
        f"✍️ <b>Author:</b> {book['clean_author']}",
    ]

    meta_lines = []
    if book.get("rating"):
        meta_lines.append(f"⭐ <b>Rating:</b> {book['rating']}/5")
    if book.get("year"):
        meta_lines.append(f"📅 <b>Year:</b> {book['year']}")
    if book.get("genres"):
        meta_lines.append(f"🏷 <b>Genres:</b> {book['genres']}")

    # Formatted description
    desc = book.get("description") or ""

    # Build buttons
    buttons: List[List[InlineKeyboardButton]] = []
    files = book.get("files", [])

    if not files:
        format_note = "\n<b>Available Formats:</b>\n<i>No downloadable files currently indexed.</i>"
    else:
        format_note = "\n<b>Available Formats:</b>"
        row = []
        for f in files:
            btn_text = format_button_label(f)
            row.append(InlineKeyboardButton(text=btn_text, callback_data=f"dl:{f['id']}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    # Navigation back button
    if query_id > 0:
        buttons.append([
            InlineKeyboardButton(text="⬅️ Back to Results", callback_data=f"page:{query_id}:{page}")
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    # Full text version (for text messages)
    full_text_lines = header_lines + meta_lines
    if desc:
        short_desc = desc[:450].rstrip() + ("..." if len(desc) > 450 else "")
        full_text_lines.append(f"\n📝 <b>Description:</b>\n<i>{short_desc}</i>")
    full_text_lines.append(format_note)
    full_text = "\n".join(full_text_lines)

    # Caption version (must strictly fit inside Telegram's 1024 char photo caption limit)
    caption_lines = header_lines + meta_lines
    if desc:
        short_desc_cap = desc[:280].rstrip() + ("..." if len(desc) > 280 else "")
        caption_lines.append(f"\n📝 <b>Description:</b>\n<i>{short_desc_cap}</i>")
    caption_lines.append(format_note)
    caption_text = "\n".join(caption_lines)

    return BookCardPayload(
        text=full_text,
        caption_text=caption_text,
        markup=markup,
        cover_url=book.get("cover_url")
    )


def build_search_results_markup(results: List[Dict[str, Any]], query_id: int, page: int, total: int) -> InlineKeyboardMarkup:
    """Builds paginated search result list with sanitized format badges."""
    buttons: List[List[InlineKeyboardButton]] = []

    # Book title buttons
    for book in results:
        fmts_badge = format_display_badges(book.get("formats", []))
        badge_str = f" [{fmts_badge}]" if fmts_badge else ""
        btn_text = f"📖 {book['clean_title']} - {book['clean_author']}{badge_str}"
        if len(btn_text) > 55:
            btn_text = btn_text[:52].rstrip() + "..."
        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"book:{book['id']}:{query_id}:{page}")
        ])

    # Pagination controls
    total_pages = math.ceil(total / PAGE_SIZE)
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="◀️ Prev", callback_data=f"page:{query_id}:{page - 1}"))

    nav_row.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data="noop"))

    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"page:{query_id}:{page + 1}"))

    if len(nav_row) > 1:
        buttons.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def ensure_book_cover(book: Dict[str, Any]) -> None:
    """Ensures book has a cover URL by querying Google Books / Open Library on demand if missing."""
    if not book.get("cover_url"):
        try:
            enriched = await enrich_book_metadata(book["clean_title"], book["clean_author"])
            if enriched.cover_url:
                book["cover_url"] = enriched.cover_url
                if not book.get("description") and enriched.description:
                    book["description"] = enriched.description
                if not book.get("rating") and enriched.rating:
                    book["rating"] = enriched.rating
                if not book.get("year") and enriched.year:
                    book["year"] = enriched.year
                if not book.get("genres") and enriched.genres:
                    book["genres"] = enriched.genres

                await update_book_metadata(
                    book_id=book["id"],
                    cover_url=enriched.cover_url,
                    description=book.get("description"),
                    rating=book.get("rating"),
                    genres=book.get("genres"),
                    year=book.get("year")
                )
                logger.info(f"Dynamically enriched cover for book ID {book['id']}")
        except Exception as e:
            logger.warning(f"On-demand cover lookup error for book {book['id']}: {e}")


@router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject) -> None:
    """Handles /start and /start book_<id> deep links."""
    user = message.from_user
    if user:
        await register_or_update_user(user.id, user.username)

    args = command.args
    # Handle deep link: /start book_<id>
    if args and args.startswith("book_"):
        raw_id = args.split("book_")[1]
        if raw_id.isdigit():
            book_id = int(raw_id)
            book = await get_book_by_id(book_id)
            if book:
                await ensure_book_cover(book)
                card = render_book_card(book)
                if card.cover_url:
                    try:
                        await message.answer_photo(
                            photo=card.cover_url,
                            caption=card.caption_text,
                            parse_mode="HTML",
                            reply_markup=card.markup
                        )
                        return
                    except Exception as img_err:
                        logger.warning(f"Failed to send cover photo ({img_err}), falling back to text.")

                await message.answer(card.text, parse_mode="HTML", reply_markup=card.markup)
                return
            else:
                await message.answer("⚠️ Sorry, that book could not be found.")
                return

    welcome_text = (
        "📚 <b>Welcome to the Digital Library & Audiobook Indexer!</b>\n\n"
        "🔍 <b>How to Search:</b>\n"
        "• Send any title or author name directly in this chat.\n"
        "• Or use inline mode anywhere by typing <code>@AudioSoulBot title</code>.\n\n"
        "⚡ All files are delivered instantly in native formats (Audiobooks, EPUB, PDF)!\n\n"
        '👨‍💻 Developed by <a href="https://t.me/souldumpp">Soul</a>'
    )
    welcome_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Developer", url="https://t.me/souldumpp")
            ]
        ]
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=welcome_markup)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    """Provides user guide on searching books and accessing formats."""
    help_text = (
        "📖 <b>Digital Library Help & Guide</b>\n\n"
        "🔍 <b>Direct Chat Search:</b>\n"
        "Send any title or author name directly in this chat to browse indexed results.\n\n"
        "⚡ <b>Inline Mode Search:</b>\n"
        "In any chat or group, type:\n"
        "<code>@AudioSoulBot title</code>\n"
        "Tap a result to view formats and download files.\n\n"
        "🎧 <b>Supported Media Formats:</b>\n"
        "• <b>Audiobooks:</b> Native `.m4b` / `.mp3` files & multi-track `.zip` archives\n"
        "• <b>EPUBs:</b> Optimized digital books for e-readers & mobile\n"
        "• <b>PDFs:</b> Documents and illustrated editions\n\n"
        '👨‍💻 Developed by <a href="https://t.me/souldumpp">Soul</a>'
    )
    help_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Developer", url="https://t.me/souldumpp")
            ]
        ]
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=help_markup)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_search_query(message: Message) -> None:
    """Handles direct text search queries in DM."""
    user = message.from_user
    if user:
        await register_or_update_user(user.id, user.username)

    query = message.text.strip()
    if len(query) < 2:
        await message.answer("⚠️ Please enter at least 2 characters to search.")
        return

    results, total = await search_books(query, limit=PAGE_SIZE, offset=0)
    if total == 0:
        await message.answer(f"🔍 No books found matching <b>'{query}'</b>. Try another search keyword.", parse_mode="HTML")
        return

    query_id = _store_query(query)
    markup = build_search_results_markup(results, query_id, page=1, total=total)
    await message.answer(
        f"🔍 Found <b>{total}</b> result(s) for <b>'{query}'</b>:\n<i>Select a title to view formats:</i>",
        parse_mode="HTML",
        reply_markup=markup
    )


@router.callback_query(F.data.startswith("page:"))
async def handle_page_callback(callback: CallbackQuery) -> None:
    """Handles pagination buttons with smooth photo/text message transitions."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return

    query_id = int(parts[1])
    page = int(parts[2])
    query = _get_query(query_id)

    if not query:
        await callback.answer("Search expired. Please type your query again.", show_alert=True)
        return

    offset = (page - 1) * PAGE_SIZE
    results, total = await search_books(query, limit=PAGE_SIZE, offset=offset)
    if total == 0:
        await callback.answer("No more results.")
        return

    markup = build_search_results_markup(results, query_id, page=page, total=total)
    results_text = f"🔍 Found <b>{total}</b> result(s) for <b>'{query}'</b>:\n<i>Select a title to view formats:</i>"

    if callback.message:
        # If transitioning back from a photo message, delete photo and send text
        if callback.message.photo:
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer(
                results_text,
                parse_mode="HTML",
                reply_markup=markup
            )
        else:
            try:
                await callback.message.edit_text(
                    results_text,
                    parse_mode="HTML",
                    reply_markup=markup
                )
            except Exception:
                await callback.message.answer(results_text, parse_mode="HTML", reply_markup=markup)

    await callback.answer()


@router.callback_query(F.data.startswith("book:"))
async def handle_book_detail_callback(callback: CallbackQuery) -> None:
    """Opens grouped book card with high-quality cover photo when available."""
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return

    book_id = int(parts[1])
    query_id = int(parts[2])
    page = int(parts[3])

    book = await get_book_by_id(book_id)
    if not book:
        await callback.answer("Book not found.", show_alert=True)
        return

    # Ensure cover exists
    await ensure_book_cover(book)
    card = render_book_card(book, query_id=query_id, page=page)

    if callback.message:
        if card.cover_url:
            # Deliver card as rich photo message
            try:
                # Delete previous search results message to provide a clean UX
                try:
                    await callback.message.delete()
                except Exception:
                    pass

                await callback.message.answer_photo(
                    photo=card.cover_url,
                    caption=card.caption_text,
                    parse_mode="HTML",
                    reply_markup=card.markup
                )
                await callback.answer()
                return
            except Exception as img_err:
                logger.warning(f"Could not send photo card ({img_err}), falling back to text.")

        # Fallback to text message
        try:
            await callback.message.edit_text(card.text, parse_mode="HTML", reply_markup=card.markup)
        except Exception:
            await callback.message.answer(card.text, parse_mode="HTML", reply_markup=card.markup)

    await callback.answer()


@router.callback_query(F.data.startswith("dl:"))
async def handle_download_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Delivers stored media file instantly via Telegram file_id without server bandwidth."""
    parts = callback.data.split(":")
    if len(parts) != 2:
        await callback.answer()
        return

    file_row_id = int(parts[1])
    file_record = await get_file_by_id(file_row_id)

    if not file_record:
        await callback.answer("File no longer available.", show_alert=True)
        return

    user_id = callback.from_user.id
    fmt = file_record["format"]
    fname = file_record.get("file_name") or ""
    telegram_file_id = file_record.get("file_id")
    channel_msg_id = file_record.get("channel_message_id")
    title = file_record["clean_title"]
    author = file_record["clean_author"]

    await callback.answer("⚡ Sending file...")

    delivered = False

    # Primary delivery method: copy_message from STORAGE_CHANNEL_ID if channel_message_id is present
    if channel_msg_id and config.STORAGE_CHANNEL_ID:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=config.STORAGE_CHANNEL_ID,
                message_id=channel_msg_id
            )
            delivered = True
            logger.info(
                f"Delivered {fmt} file {file_row_id} via copy_message "
                f"(message_id={channel_msg_id}, channel={config.STORAGE_CHANNEL_ID}) to user {user_id}"
            )
        except Exception as copy_err:
            logger.warning(
                f"copy_message failed for message_id={channel_msg_id} ({copy_err}), falling back to direct delivery."
            )

    # Fallback delivery method: send_audio or send_document using telegram_file_id
    if not delivered and telegram_file_id:
        caption = f"📖 <b>{title}</b>\n✍️ {author}"
        is_archive = is_archive_file(fname, fmt)
        try:
            if fmt == "AUDIO" and not is_archive:
                await bot.send_audio(
                    chat_id=user_id,
                    audio=telegram_file_id,
                    caption=caption,
                    title=title,
                    performer=author,
                    parse_mode="HTML"
                )
            else:
                await bot.send_document(
                    chat_id=user_id,
                    document=telegram_file_id,
                    caption=caption,
                    parse_mode="HTML"
                )
            delivered = True
            logger.info(f"Delivered {fmt} file {file_row_id} via send_audio/send_document to user {user_id}")
        except Exception as send_err:
            logger.error(f"Error sending file {telegram_file_id} to {user_id}: {send_err}")

    if delivered:
        await increment_user_downloads(user_id)
    else:
        if callback.message:
            await callback.message.answer("⚠️ Failed to deliver file. Please check back later.")


@router.callback_query(F.data == "noop")
async def handle_noop_callback(callback: CallbackQuery) -> None:
    """No-op for page indicator button."""
    await callback.answer()
