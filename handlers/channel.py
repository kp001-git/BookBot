import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from config import settings
from utils.parser import parse_media_metadata
from utils.enricher import enrich_book_metadata, format_duration, format_size
from database.models import upsert_book_and_file, update_book_metadata

logger = logging.getLogger(__name__)
router = Router(name="channel_router")


def is_storage_channel(chat_id: int | str) -> bool:
    """Validates STORAGE_CHANNEL_ID flexibly (string comparison, int casting, and -100 prefix tolerance)."""
    target = settings.STORAGE_CHANNEL_ID
    if target is None:
        return False
    c_str = str(chat_id).strip()
    t_str = str(target).strip()
    if c_str == t_str:
        return True
    try:
        if int(c_str) == int(t_str):
            return True
    except (ValueError, TypeError):
        pass
    c_norm = c_str.removeprefix("-100").removeprefix("-")
    t_norm = t_str.removeprefix("-100").removeprefix("-")
    return bool(c_norm and c_norm == t_norm)


@router.channel_post()
@router.edited_channel_post()
async def handle_storage_channel_post(message: Message, bot: Bot) -> None:
    """Listens to channel posts in STORAGE_CHANNEL_ID, sanitizes metadata, and triggers enrichment & broadcast."""
    logger.info(
        f"Incoming channel post from chat ID: {message.chat.id}, "
        f"has_audio: {bool(message.audio)}, has_document: {bool(message.document)}"
    )

    if not is_storage_channel(message.chat.id):
        logger.warning(
            f"Ignored channel post: chat ID {message.chat.id} does not match configured "
            f"STORAGE_CHANNEL_ID ({settings.STORAGE_CHANNEL_ID})."
        )
        return

    is_audio = message.audio is not None
    is_doc = message.document is not None

    if not is_audio and not is_doc:
        logger.debug(f"Ignored post in storage channel {message.chat.id}: neither audio nor document.")
        return

    # Extract raw attributes
    file_unique_id = None
    if is_audio:
        audio = message.audio
        file_id = audio.file_id
        file_unique_id = getattr(audio, "file_unique_id", None)
        file_name = audio.file_name
        file_size = audio.file_size
        mime_type = audio.mime_type
        duration = audio.duration
        audio_title = audio.title
        audio_performer = audio.performer
    else:
        doc = message.document
        file_id = doc.file_id
        file_unique_id = getattr(doc, "file_unique_id", None)
        file_name = doc.file_name
        file_size = doc.file_size
        mime_type = doc.mime_type
        duration = None
        audio_title = None
        audio_performer = None

    caption = message.caption

    # Parse and clean metadata
    parsed = parse_media_metadata(
        file_name=file_name,
        caption=caption,
        audio_title=audio_title,
        audio_performer=audio_performer,
        mime_type=mime_type,
        is_audio_msg=is_audio
    )

    logger.info(
        f"Indexing file: '{parsed.clean_title}' by '{parsed.clean_author}' "
        f"[{parsed.format_type}] (Canonical: '{parsed.canonical_title}')"
    )

    # Database ingestion
    book_id, is_new_book, is_new_file = await upsert_book_and_file(
        clean_title=parsed.clean_title,
        clean_author=parsed.clean_author,
        canonical_title=parsed.canonical_title,
        canonical_author=parsed.canonical_author,
        format_type=parsed.format_type,
        file_id=file_id,
        file_size=file_size,
        file_name=parsed.file_name,
        duration=duration,
        channel_message_id=message.message_id,
        file_unique_id=file_unique_id
    )

    if not is_new_file:
        logger.info(f"File '{file_id}' already indexed for book ID {book_id}.")
        return

    logger.info(
        f"Committed to database: book_id={book_id}, is_new_book={is_new_book}, "
        f"is_new_file={is_new_file}, title='{parsed.clean_title}'"
    )

    # Enrichment pipeline (for new books or whenever indexed)
    cover_url = None
    description = None
    rating = None
    year = None
    genres = None

    if is_new_book:
        try:
            enriched = await enrich_book_metadata(parsed.clean_title, parsed.clean_author)
            cover_url = enriched.cover_url
            description = enriched.description
            rating = enriched.rating
            year = enriched.year
            genres = enriched.genres

            await update_book_metadata(
                book_id=book_id,
                cover_url=cover_url,
                description=description,
                rating=rating,
                genres=genres,
                year=year
            )
            logger.info(f"Enriched book ID {book_id} with API metadata.")
        except Exception as e:
            logger.warning(f"Error enriching book {book_id}: {e}")

    # Broadcast to Updates Channel
    if settings.UPDATES_CHANNEL_ID:
        try:
            bot_user = await bot.get_me()
            bot_username = settings.BOT_USERNAME or bot_user.username
            deep_link = f"https://t.me/{bot_username}?start=book_{book_id}"

            dur_str = format_duration(duration) if duration else None
            size_str = format_size(file_size)

            lines = ["📢 #NewRelease Added!", ""]
            lines.append(f"📖 <b>Title:</b> {parsed.clean_title}")
            # Format display label
            fname_lower = (parsed.file_name or "").lower()
            is_arch = any(fname_lower.endswith(ext) for ext in [".zip", ".rar", ".tar", ".7z", ".gz"])
            if parsed.format_type == "AUDIO":
                format_display = "Audiobook (ZIP)" if is_arch else "Audiobook"
            else:
                format_display = parsed.format_type

            lines.append(f"📁 <b>Format:</b> {format_display}")

            meta_details = []
            if dur_str:
                meta_details.append(f"⏱ <b>Duration:</b> {dur_str}")
            if size_str != "N/A":
                meta_details.append(f"📦 <b>Size:</b> {size_str}")
            if meta_details:
                lines.append(" | ".join(meta_details))

            if rating:
                lines.append(f"⭐ <b>Rating:</b> {rating}/5")
            if year:
                lines.append(f"📅 <b>Year:</b> {year}")
            if genres:
                lines.append(f"🏷 <b>Genres:</b> {genres}")

            if description:
                desc_snippet = description[:350].rstrip() + ("..." if len(description) > 350 else "")
                lines.append(f"\n📝 <b>Description:</b>\n<i>{desc_snippet}</i>")

            broadcast_text = "\n".join(lines)

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="📥 Download in Bot", url=deep_link)
                ]]
            )

            if cover_url:
                try:
                    await bot.send_photo(
                        chat_id=settings.UPDATES_CHANNEL_ID,
                        photo=cover_url,
                        caption=broadcast_text,
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
                except Exception as img_err:
                    logger.warning(f"Failed to broadcast with photo ({img_err}), falling back to text.")
                    await bot.send_message(
                        chat_id=settings.UPDATES_CHANNEL_ID,
                        text=broadcast_text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=True
                    )
            else:
                await bot.send_message(
                    chat_id=settings.UPDATES_CHANNEL_ID,
                    text=broadcast_text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
            logger.info(f"Broadcasted book {book_id} to updates channel {settings.UPDATES_CHANNEL_ID}")
        except Exception as b_err:
            logger.error(f"Failed to broadcast update to channel: {b_err}")
