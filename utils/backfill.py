"""
utils/backfill.py
Standalone CLI script to read historical channel messages from STORAGE_CHANNEL_ID
and index all past audiobooks and media files into the database using Telethon.
Optimized for high-throughput ingestion (>100 items/sec) with synchronous API enrichment bypassed.
"""

import os
import sys
import argparse
import asyncio
import logging
from typing import Optional, List, Dict, Any

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient
from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    DocumentAttributeAudio
)
import telethon.utils

from config import config
from utils.parser import parse_media_metadata
from database.connection import set_db_path, set_database_url, get_db_engine_name, close_db
from database.models import (
    init_db,
    insert_or_update_book,
    upsert_book_and_file,
    batch_upsert_books_and_files,
    is_file_indexed,
    get_indexed_message_ids
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("backfill")


async def run_backfill(
    api_id: int,
    api_hash: str,
    channel_id: int,
    phone: Optional[str] = None,
    session_name: str = "backfill_session",
    bot_token: Optional[str] = None,
    limit: Optional[int] = None,
    skip_enrich: bool = True,
    enrich: Optional[bool] = None,
    batch_size: int = 1
) -> dict:
    """
    Connects to Telegram via Telethon, iterates historical channel messages,
    extracts metadata via parse_media_metadata, and inserts directly into library.db.
    External HTTP enrichment is bypassed by default for ultra-fast scanning (>100 items/sec).
    """
    if enrich is not None:
        skip_enrich = not enrich

    logger.info("Initializing database...")
    if config.DATABASE_URL:
        set_database_url(config.DATABASE_URL)
    else:
        set_db_path(config.DB_PATH)
    await init_db()

    logger.info(f"Active Database Engine: {get_db_engine_name()} (WAL mode active)")
    logger.info(f"Target Storage Channel ID: {channel_id}")
    logger.info(f"Scan limit: {limit or 'ALL messages'}")
    logger.info(f"Skip Enrichment: {skip_enrich} (External HTTP calls bypassed)")
    logger.info(f"Batch Size: {batch_size}")

    # Preload indexed message IDs into memory for O(1) deduplication
    indexed_message_ids = set()
    try:
        indexed_message_ids = await get_indexed_message_ids()
        logger.info(f"Preloaded {len(indexed_message_ids)} already-indexed message IDs for instant O(1) deduplication.")
    except Exception as e:
        logger.warning(f"Could not preload indexed message IDs: {e}")

    client = TelegramClient(session_name, api_id, api_hash)
    if bot_token:
        logger.info("Connecting via Bot Token authentication...")
        await client.start(bot_token=bot_token)
    elif phone:
        logger.info(f"Connecting via User Phone authentication ({phone})...")
        await client.start(phone=phone)
    else:
        logger.info("Connecting via User Session (interactive)...")
        await client.start()

    logger.info("Connected to Telegram successfully.")

    metrics = {
        "scanned": 0,
        "indexed_new_files": 0,
        "already_indexed": 0,
        "non_media": 0,
        "errors": 0
    }

    t_start = asyncio.get_event_loop().time()
    batch_buffer: List[Dict[str, Any]] = []

    try:
        # Prime entity cache by fetching dialogs
        logger.info("Fetching dialogs to prime channel cache...")
        dialogs = await client.get_dialogs()

        # Try getting entity with normalization fallbacks
        target_channel = None
        try:
            target_channel = await client.get_entity(channel_id)
        except Exception as e:
            logger.debug(f"Direct get_entity({channel_id}) failed ({e}). Searching dialogs...")
            clean_id_str = str(channel_id).replace("-100", "").replace("-", "")
            clean_id = int(clean_id_str) if clean_id_str.isdigit() else None
            for dialog in dialogs:
                d_id = dialog.id
                d_entity_id = getattr(dialog.entity, "id", None)
                matches = {channel_id}
                if clean_id is not None:
                    matches.update({clean_id, -clean_id, int(f"-100{clean_id}")})
                if d_id in matches or d_entity_id in matches:
                    target_channel = dialog.entity
                    break

        if not target_channel:
            raise ValueError(
                f"Account has not joined channel {channel_id}. Please join the channel with this account first."
            )

        logger.info(f"Successfully resolved target channel: {getattr(target_channel, 'title', target_channel)}")

        async for message in client.iter_messages(target_channel, limit=limit):
            metrics["scanned"] += 1

            if not message or not message.media:
                metrics["non_media"] += 1
                continue

            channel_message_id = message.id

            # Instant in-memory check to avoid redundant disk reads
            if channel_message_id in indexed_message_ids:
                metrics["already_indexed"] += 1
                if metrics["scanned"] % 100 == 0:
                    elapsed = asyncio.get_event_loop().time() - t_start
                    rate = metrics["scanned"] / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"Progress: {metrics['scanned']} msgs scanned | "
                        f"{metrics['indexed_new_files']} new indexed | "
                        f"{metrics['already_indexed']} skipped | "
                        f"Rate: {rate:.1f} msgs/sec"
                    )
                continue

            # Extract Document object (covers audio, epubs, pdfs, and zip archives)
            doc = None
            if hasattr(message, "document") and message.document:
                doc = message.document
            elif hasattr(message.media, "document") and message.media.document:
                doc = message.media.document
            elif isinstance(message.media, Document):
                doc = message.media

            if not doc:
                metrics["non_media"] += 1
                continue

            file_unique_id = f"doc_{doc.id}"

            # Secondary database index check if preloaded set was incomplete
            if await is_file_indexed(channel_message_id=channel_message_id, file_unique_id=file_unique_id):
                metrics["already_indexed"] += 1
                indexed_message_ids.add(channel_message_id)
                continue

            # Convert to Bot API file_id for direct delivery fallback
            try:
                file_id = telethon.utils.pack_bot_file_id(doc)
            except Exception:
                file_id = None

            if not file_id:
                file_id = f"telethon_doc_{doc.id}"

            file_size = getattr(doc, "size", None)
            mime_type = getattr(doc, "mime_type", None)

            # Inspect attributes for audio details and filename
            file_name = None
            duration = None
            audio_title = None
            audio_performer = None
            is_audio_msg = False

            if hasattr(doc, "attributes") and doc.attributes:
                for attr in doc.attributes:
                    if isinstance(attr, DocumentAttributeFilename):
                        file_name = attr.file_name
                    elif isinstance(attr, DocumentAttributeAudio):
                        is_audio_msg = True
                        duration = attr.duration
                        audio_title = attr.title
                        audio_performer = attr.performer

            if not file_name and hasattr(message, "file") and message.file and message.file.name:
                file_name = message.file.name

            caption = message.message or message.raw_text or ""

            # Parse and sanitize metadata
            try:
                parsed = parse_media_metadata(
                    file_name=file_name,
                    caption=caption,
                    audio_title=audio_title,
                    audio_performer=audio_performer,
                    mime_type=mime_type,
                    is_audio_msg=is_audio_msg
                )

                if batch_size > 1:
                    batch_buffer.append({
                        "clean_title": parsed.clean_title,
                        "clean_author": parsed.clean_author,
                        "canonical_title": parsed.canonical_title,
                        "canonical_author": parsed.canonical_author,
                        "format_type": parsed.format_type,
                        "file_id": file_id,
                        "file_size": file_size,
                        "file_name": parsed.file_name,
                        "duration": duration,
                        "channel_message_id": channel_message_id,
                        "file_unique_id": file_unique_id,
                        "cover_url": None,
                        "description": None,
                    })
                    indexed_message_ids.add(channel_message_id)

                    if len(batch_buffer) >= batch_size:
                        _, new_files = await batch_upsert_books_and_files(batch_buffer)
                        metrics["indexed_new_files"] += new_files
                        batch_buffer.clear()
                else:
                    # Direct insert into database.models.insert_or_update_book with cover_url=None, description=None
                    book_id, is_new_book, is_new_file = await insert_or_update_book(
                        clean_title=parsed.clean_title,
                        clean_author=parsed.clean_author,
                        canonical_title=parsed.canonical_title,
                        canonical_author=parsed.canonical_author,
                        format_type=parsed.format_type,
                        file_id=file_id,
                        file_size=file_size,
                        file_name=parsed.file_name,
                        duration=duration,
                        channel_message_id=channel_message_id,
                        file_unique_id=file_unique_id,
                        cover_url=None,
                        description=None
                    )
                    indexed_message_ids.add(channel_message_id)

                    if is_new_file:
                        metrics["indexed_new_files"] += 1
                        logger.info(
                            f"[{metrics['indexed_new_files']}] Indexed (msg #{channel_message_id}): "
                            f"'{parsed.clean_title}' by '{parsed.clean_author}' [{parsed.format_type}] (Book ID: {book_id})"
                        )
                        # Optional enrichment only if explicitly requested (default skip_enrich is True)
                        if not skip_enrich and is_new_book:
                            try:
                                from utils.enricher import enrich_book_metadata
                                from database.models import update_book_metadata
                                enriched = await enrich_book_metadata(parsed.clean_title, parsed.clean_author)
                                if (
                                    enriched.cover_url
                                    or enriched.description
                                    or enriched.rating
                                    or enriched.genres
                                    or enriched.year
                                ):
                                    await update_book_metadata(
                                        book_id=book_id,
                                        cover_url=enriched.cover_url,
                                        description=enriched.description,
                                        rating=enriched.rating,
                                        genres=enriched.genres,
                                        year=enriched.year
                                    )
                            except Exception as enrich_err:
                                logger.debug(f"Enrichment note for book {book_id}: {enrich_err}")
                    else:
                        metrics["already_indexed"] += 1

            except Exception as e:
                metrics["errors"] += 1
                logger.error(f"Error parsing message ID {message.id}: {e}")

            # Progress logging every 100 messages
            if metrics["scanned"] % 100 == 0:
                elapsed = asyncio.get_event_loop().time() - t_start
                rate = metrics["scanned"] / elapsed if elapsed > 0 else 0
                logger.info(
                    f"Progress: {metrics['scanned']} msgs scanned | "
                    f"{metrics['indexed_new_files']} new indexed | "
                    f"{metrics['already_indexed']} skipped | "
                    f"Rate: {rate:.1f} msgs/sec"
                )

        # Flush any remaining items in batch buffer
        if batch_buffer:
            _, new_files = await batch_upsert_books_and_files(batch_buffer)
            metrics["indexed_new_files"] += new_files
            batch_buffer.clear()

    finally:
        await client.disconnect()
        await close_db()

    t_total = asyncio.get_event_loop().time() - t_start
    overall_rate = metrics["scanned"] / t_total if t_total > 0 else 0

    logger.info("=" * 60)
    logger.info("🎉 Historical Channel Backfill Completed!")
    logger.info(f"Total messages scanned: {metrics['scanned']}")
    logger.info(f"Newly indexed files:    {metrics['indexed_new_files']}")
    logger.info(f"Already indexed files:  {metrics['already_indexed']}")
    logger.info(f"Non-media messages:     {metrics['non_media']}")
    logger.info(f"Errors encountered:     {metrics['errors']}")
    logger.info(f"Total time elapsed:     {t_total:.2f}s ({overall_rate:.1f} msgs/sec)")
    logger.info("=" * 60)
    return metrics


def main():
    parser = argparse.ArgumentParser(
        description="One-time script to backfill existing storage channel files into library.db via Telethon."
    )
    parser.add_argument(
        "--api-id",
        type=int,
        default=config.TELEGRAM_API_ID,
        help="Telegram API ID from https://my.telegram.org (or TELEGRAM_API_ID in .env)"
    )
    parser.add_argument(
        "--api-hash",
        type=str,
        default=config.TELEGRAM_API_HASH,
        help="Telegram API Hash from https://my.telegram.org (or TELEGRAM_API_HASH in .env)"
    )
    parser.add_argument(
        "--phone",
        type=str,
        default=config.TELEGRAM_PHONE,
        help="Phone number associated with user account (or TELEGRAM_PHONE in .env)"
    )
    parser.add_argument(
        "--channel-id",
        type=int,
        default=config.STORAGE_CHANNEL_ID,
        help="Storage channel ID (defaults to STORAGE_CHANNEL_ID in .env)"
    )
    parser.add_argument(
        "--session",
        type=str,
        default="backfill_session",
        help="Session filename for Telethon (default: 'backfill_session')"
    )
    parser.add_argument(
        "--bot",
        action="store_true",
        help="Authenticate using BOT_TOKEN instead of user phone session"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of historical messages to scan (default: all)"
    )
    parser.add_argument(
        "--skip-enrich",
        action="store_true",
        default=True,
        help="Skip external Open Library/Google Books API enrichment (default: True, ultra-fast backfill)"
    )
    parser.add_argument(
        "--no-skip-enrich",
        dest="skip_enrich",
        action="store_false",
        help="Do not skip enrichment (enable Open Library/Google Books API enrichment)"
    )
    parser.add_argument(
        "--enrich",
        dest="skip_enrich",
        action="store_false",
        help="Enable external API enrichment (warning: slow due to Open Library/Google Books timeouts)"
    )
    parser.add_argument(
        "--no-enrich",
        dest="skip_enrich",
        action="store_true",
        help="Alias for --skip-enrich"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="Batch size for database writes (default: 1 in WAL mode; set >1 for buffered batching)"
    )

    args = parser.parse_args()

    api_id = args.api_id
    api_hash = args.api_hash

    if not api_id:
        val = input("Enter your Telegram API ID (from https://my.telegram.org): ").strip()
        if val.isdigit():
            api_id = int(val)
        else:
            print("❌ Invalid API ID. Must be an integer.")
            sys.exit(1)

    if not api_hash:
        api_hash = input("Enter your Telegram API Hash: ").strip()
        if not api_hash:
            print("❌ Invalid API Hash.")
            sys.exit(1)

    channel_id = args.channel_id
    if not channel_id:
        print("❌ STORAGE_CHANNEL_ID is not configured in .env or passed via --channel-id.")
        sys.exit(1)

    bot_token = config.BOT_TOKEN if args.bot else None

    asyncio.run(
        run_backfill(
            api_id=api_id,
            api_hash=api_hash,
            channel_id=channel_id,
            phone=args.phone,
            session_name=args.session,
            bot_token=bot_token,
            limit=args.limit,
            skip_enrich=args.skip_enrich,
            batch_size=args.batch_size
        )
    )


if __name__ == "__main__":
    main()
