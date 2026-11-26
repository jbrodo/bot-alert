"""Receive monitoring messages from the Telegram Bot API."""

import asyncio
import logging
import os

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import Update
from psycopg import AsyncConnection

from app.parser.telegram import parse_telegram_message
from app.storage import initialize, record_notification

logger = logging.getLogger(__name__)


async def handle_update(
    update: Update, connection: AsyncConnection, *, chat_id: int, monitor_bot_id: int
) -> None:
    message = update.message
    if not message or message.chat.id != chat_id or message.text is None:
        return
    sender = message.from_user
    if message.sender_chat or not sender or not sender.is_bot or sender.id != monitor_bot_id:
        return

    parsed = parse_telegram_message(message.text)
    if await record_notification(
        connection,
        chat_id=chat_id,
        message_id=message.message_id,
        sender_id=sender.id,
        sent_at=message.date,
        text=message.text,
        parsed=parsed,
    ):
        logger.info(
            "Saved Telegram message %s (%s)",
            message.message_id,
            parsed.status.value if parsed else "unparsed",
        )


async def poll(
    bot: Bot, connection: AsyncConnection, *, chat_id: int, monitor_bot_id: int
) -> None:
    offset = None
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=["message"])
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)
            continue
        except (TelegramNetworkError, TelegramServerError) as error:
            logger.warning("Telegram polling will retry (%s)", type(error).__name__)
            await asyncio.sleep(5)
            continue

        # A successful response has already confirmed the previous offset.
        offset = None
        for update in updates:
            # Advance only after the transaction commits. Restart on storage errors.
            await handle_update(update, connection, chat_id=chat_id, monitor_bot_id=monitor_bot_id)
            offset = update.update_id + 1


async def run() -> None:
    database_url = os.environ["DATABASE_URL"]
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    try:
        chat_id = int(os.environ["TELEGRAM_CHAT_ID"])
        monitor_bot_id = int(os.environ["TELEGRAM_MONITOR_BOT_ID"])
    except ValueError as error:
        raise SystemExit("TELEGRAM_CHAT_ID and TELEGRAM_MONITOR_BOT_ID must be integers") from error

    async with await AsyncConnection.connect(database_url) as connection:
        await initialize(connection)
        async with Bot(token) as bot:
            logger.info("Polling Telegram chat %s for bot %s", chat_id, monitor_bot_id)
            await poll(bot, connection, chat_id=chat_id, monitor_bot_id=monitor_bot_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run())
    except KeyError as error:
        raise SystemExit(f"Missing environment variable: {error.args[0]}") from None
    except KeyboardInterrupt:
        pass
