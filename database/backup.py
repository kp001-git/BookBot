import os
import asyncio
import logging
import sqlite3
import tempfile
from datetime import datetime, timezone
from typing import Optional
from aiogram import Bot
from aiogram.types import FSInputFile

from config import settings
from database.connection import get_db_path, is_postgres

logger = logging.getLogger(__name__)


def create_sqlite_snapshot_sync(src_path: str, dst_path: str) -> None:
    """Safely copies SQLite DB including committed WAL data using SQLite backup API."""
    if os.path.exists(dst_path):
        os.remove(dst_path)
    src_conn = sqlite3.connect(src_path)
    dst_conn = sqlite3.connect(dst_path)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


async def create_sqlite_backup_file() -> str:
    """Creates a local temporary snapshot of the SQLite database."""
    db_path = get_db_path()
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite database file not found at {db_path}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    temp_dir = tempfile.gettempdir()
    backup_path = os.path.join(temp_dir, f"library_backup_{timestamp}.db")

    await asyncio.to_thread(create_sqlite_snapshot_sync, db_path, backup_path)
    return backup_path


async def send_database_backup(bot: Bot, target_chat_id: Optional[int] = None) -> bool:
    """
    Creates a vacuum snapshot copy of SQLite and sends it as a Telegram document.
    Caption: #DatabaseBackup - YYYY-MM-DD HH:MM:SS
    """
    if is_postgres():
        logger.info("Database is PostgreSQL (cloud-persistent). SQLite automated backup skipped.")
        return True

    dest_id = target_chat_id
    if dest_id is None:
        if settings.ADMIN_IDS and len(settings.ADMIN_IDS) > 0:
            dest_id = settings.ADMIN_IDS[0]
        else:
            logger.warning("No admin ID configured; cannot deliver SQLite backup.")
            return False

    backup_path = None
    try:
        backup_path = await create_sqlite_backup_file()
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        caption = f"#DatabaseBackup - {timestamp_str}"
        doc = FSInputFile(backup_path, filename=f"library_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")

        await bot.send_document(
            chat_id=dest_id,
            document=doc,
            caption=caption
        )
        logger.info(f"Successfully dispatched database backup to chat {dest_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to generate or send SQLite backup: {e}", exc_info=True)
        raise
    finally:
        if backup_path and os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except Exception:
                pass


async def sqlite_backup_worker(bot: Bot) -> None:
    """Background asynchronous task running every 6 hours to backup SQLite to Telegram."""
    logger.info("SQLite periodic backup worker started (interval: 6h).")
    while True:
        try:
            await asyncio.sleep(6 * 3600)  # 6 hours
            if not is_postgres() and settings.ADMIN_IDS:
                logger.info("Triggering scheduled 6-hour SQLite backup...")
                await send_database_backup(bot)
        except asyncio.CancelledError:
            logger.info("SQLite backup worker task cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in sqlite_backup_worker: {e}", exc_info=True)
