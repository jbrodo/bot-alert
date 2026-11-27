"""Manage open monitoring tickets and report changes to the configured chat."""

import asyncio
import logging
import os

from aiogram import Bot
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from app.incidents import PROJECT_LOCK
from app.storage import initialize
from app.telegram.laya import DecisionUnavailable, decide
from app.tickets import close_ticket

logger = logging.getLogger(__name__)
MANAGER_LOCK = 0x414C45524D
ACTION_LABELS = {
    "investigate": "verificare l'alert",
    "verify_recovery": "verificare il ripristino",
    "review": "verificare lo stato",
}


async def initialize_chat(connection: AsyncConnection, chat_id: int) -> None:
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (MANAGER_LOCK,))
        cursor = await connection.execute(
            "INSERT INTO ticket_manager_chats (chat_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING chat_id",
            (chat_id,),
        )
        if not await cursor.fetchone():
            return
        await connection.execute(
            """
            INSERT INTO ticket_manager_events (chat_id, message_id, position)
            SELECT chat_id, message_id, position FROM (
                SELECT e.chat_id, e.message_id, e.position, i.closed_at,
                       row_number() OVER (
                           PARTITION BY ie.incident_id
                           ORDER BY n.sent_at DESC, e.message_id DESC, e.position DESC
                       ) AS latest
                FROM incident_events ie
                JOIN alert_events e USING (chat_id, message_id, position)
                JOIN telegram_notifications n USING (chat_id, message_id)
                JOIN incidents i ON i.id = ie.incident_id
                WHERE i.chat_id = %s
            ) events
            WHERE closed_at IS NOT NULL OR latest > 1
            ON CONFLICT DO NOTHING
            """,
            (chat_id,),
        )


async def process_next(connection: AsyncConnection, bot: Bot, chat_id: int) -> bool:
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (MANAGER_LOCK,))
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT e.chat_id, e.message_id, e.position, e.status, e.summary,
                       n.sent_at, n.severity, i.id AS incident_id, t.id AS ticket_id
                FROM incident_events ie
                JOIN alert_events e USING (chat_id, message_id, position)
                JOIN telegram_notifications n USING (chat_id, message_id)
                JOIN incidents i ON i.id = ie.incident_id
                JOIN tickets t ON t.incident_id = i.id
                LEFT JOIN ticket_manager_events done
                    ON (done.chat_id, done.message_id, done.position)
                    = (e.chat_id, e.message_id, e.position)
                WHERE i.chat_id = %s AND i.closed_at IS NULL AND done.message_id IS NULL
                ORDER BY n.sent_at, e.message_id, e.position
                LIMIT 1
                """,
                (chat_id,),
            )
            event = await cursor.fetchone()
        if event is None:
            return False

        decision = await decide(event["status"], event["severity"])
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (PROJECT_LOCK,))
        cursor = await connection.execute(
            """
            SELECT e.status, n.sent_at, e.message_id, e.position
            FROM incident_events ie
            JOIN alert_events e USING (chat_id, message_id, position)
            JOIN telegram_notifications n USING (chat_id, message_id)
            WHERE ie.incident_id = %s
            ORDER BY n.sent_at DESC, e.message_id DESC, e.position DESC LIMIT 1
            """,
            (event["incident_id"],),
        )
        latest = await cursor.fetchone()
        closed = False
        if event["status"] == "RESOLVED" and latest == (
            event["status"],
            event["sent_at"],
            event["message_id"],
            event["position"],
        ):
            closed = await close_ticket(
                connection,
                event["ticket_id"],
                f"Chiusura automatica dopo RESOLVED Telegram #{event['message_id']}.",
            )

        cursor = await connection.execute(
            "SELECT closed_at IS NULL FROM incidents WHERE id = %s",
            (event["incident_id"],),
        )
        incident_state = await cursor.fetchone()
        if incident_state is None:
            raise RuntimeError(f"Incidente #{event['incident_id']} non trovato")
        is_open = incident_state[0]
        status = (
            "chiuso automaticamente"
            if closed
            else ("aperto" if is_open else "già chiuso")
        )
        text = (
            f"Ticket #{event['ticket_id']} · incidente #{event['incident_id']} · {status}\n"
            f"{event['summary']}\n"
            f"Evento monitoraggio: {event['status']} · notifica #{event['message_id']}\n"
            f"Indicazione Laya: {ACTION_LABELS[decision.choice]}"
        )
        await bot.send_message(chat_id, text, disable_notification=True)
        await connection.execute(
            """
            INSERT INTO ticket_manager_events (chat_id, message_id, position, sent_at)
            VALUES (%s, %s, %s, now())
            """,
            (event["chat_id"], event["message_id"], event["position"]),
        )
        return True


async def run() -> None:
    chat_id = int(os.environ["TELEGRAM_CHAT_ID"])
    async with await AsyncConnection.connect(os.environ["DATABASE_URL"]) as connection:
        await initialize(connection)
        await initialize_chat(connection, chat_id)
        async with Bot(os.environ["TELEGRAM_BOT_TOKEN"]) as bot:
            while True:
                try:
                    if not await process_next(connection, bot, chat_id):
                        await asyncio.sleep(10)
                except DecisionUnavailable as error:
                    logger.warning("%s", error)
                    await asyncio.sleep(30)
                except TelegramRetryAfter as error:
                    logger.warning(
                        "Telegram richiede una pausa di %s secondi", error.retry_after
                    )
                    await asyncio.sleep(error.retry_after)
                except (TelegramNetworkError, TelegramServerError) as error:
                    logger.warning(
                        "Invio Telegram non riuscito (%s), nuovo tentativo",
                        type(error).__name__,
                    )
                    await asyncio.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
