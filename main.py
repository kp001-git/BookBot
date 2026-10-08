import asyncio
import logging
import os
import sys
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault

from config import settings
from database.connection import set_db_path
from database.models import init_db
from handlers.channel import router as channel_router
from handlers.search import router as search_router
from handlers.inline import router as inline_router
from handlers.admin import router as admin_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("bot")


async def start_web_server() -> web.AppRunner:
    """Runs a lightweight HTTP server so Render port check passes and UptimeRobot can keep the service awake."""
    routes = web.RouteTableDef()

    @routes.get("/")
    async def handle_root(request: web.Request) -> web.Response:
        return web.Response(
            text="🎧 AudioSoulBot is running and healthy! 📚✨",
            content_type="text/plain"
        )

    @routes.get("/health")
    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "bot": "@AudioSoulBot",
            "service": "telegram-audiobook-bot"
        })

    app = web.Application()
    app.add_routes(routes)

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info(f"Health check HTTP server listening on http://0.0.0.0:{port}")
    return runner


async def main() -> None:
    logger.info("Initializing Telegram Audiobook & Library Bot...")

    # Configure database
    set_db_path(settings.DB_PATH)
    await init_db()

    # Initialize Bot instance with HTML parse mode
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    # Initialize Dispatcher
    dp = Dispatcher()

    # Register Routers
    # Note: Order matters. Channel posts & Admin commands first, then search & inline.
    dp.include_router(channel_router)
    dp.include_router(admin_router)
    dp.include_router(search_router)
    dp.include_router(inline_router)

    # Startup hook to verify bot credentials
    web_runner: Optional[web.AppRunner] = None
    try:
        # Start background HTTP server for Render and health checks
        web_runner = await start_web_server()

        bot_info = await bot.get_me()
        if not settings.BOT_USERNAME:
            settings.BOT_USERNAME = bot_info.username
        logger.info(f"Bot started successfully as @{bot_info.username} (ID: {bot_info.id})")
        logger.info(f"Storage Channel: {settings.STORAGE_CHANNEL_ID}")
        if settings.UPDATES_CHANNEL_ID:
            logger.info(f"Updates Broadcast Channel: {settings.UPDATES_CHANNEL_ID}")
        logger.info(f"Admins: {settings.ADMIN_IDS}")

        # Register bot commands menu in Telegram
        bot_commands = [
            BotCommand(command="start", description="Start bot & search library"),
            BotCommand(command="stats", description="Admin metrics & statistics"),
        ]
        await bot.set_my_commands(bot_commands, scope=BotCommandScopeDefault())
        logger.info("Bot commands successfully registered with Telegram.")

        # Drop pending updates and start polling
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"Fatal error while running bot: {e}", exc_info=True)
    finally:
        if web_runner:
            await web_runner.cleanup()
            logger.info("Health check HTTP server stopped.")
        await bot.session.close()
        logger.info("Bot session closed. Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
