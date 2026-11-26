"""PostgreSQL storage for Telegram notifications and their alert events."""

from datetime import datetime

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from app.parser.telegram import TelegramNotification


async def initialize(connection: AsyncConnection) -> None:
    async with connection.transaction():
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_notifications (
                chat_id bigint NOT NULL,
                message_id bigint NOT NULL,
                sender_id bigint NOT NULL,
                sent_at timestamptz NOT NULL,
                raw_text text NOT NULL,
                alertname text,
                severity text,
                group_status text,
                PRIMARY KEY (chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS alert_events (
                chat_id bigint NOT NULL,
                message_id bigint NOT NULL,
                position integer NOT NULL,
                status text,
                summary text NOT NULL,
                description text NOT NULL,
                resource jsonb NOT NULL,
                metrics jsonb NOT NULL,
                PRIMARY KEY (chat_id, message_id, position),
                FOREIGN KEY (chat_id, message_id)
                    REFERENCES telegram_notifications (chat_id, message_id)
            );
            """
        )


async def record_notification(
    connection: AsyncConnection,
    *,
    chat_id: int,
    message_id: int,
    sender_id: int,
    sent_at: datetime,
    text: str,
    parsed: TelegramNotification | None,
) -> bool:
    """Commit an original message and its derived events together."""
    async with connection.transaction():
        inserted = await connection.execute(
            """
            INSERT INTO telegram_notifications
                (chat_id, message_id, sender_id, sent_at, raw_text, alertname, severity, group_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, message_id) DO NOTHING
            RETURNING message_id
            """,
            (
                chat_id, message_id, sender_id, sent_at, text,
                parsed.alertname if parsed else None,
                parsed.severity if parsed else None,
                parsed.status.value if parsed else None,
            ),
        )
        if await inserted.fetchone() is None:
            return False

        if parsed:
            rows = []
            for position, alert in enumerate(parsed.alerts):
                rows.append(
                    (
                        chat_id, message_id, position,
                        alert.status.value if alert.status else None,
                        alert.summary, alert.description,
                        Jsonb(alert.resource.model_dump(exclude_none=True)),
                        Jsonb([metric.model_dump(mode="json") for metric in alert.metrics]),
                    )
                )
            async with connection.cursor() as cursor:
                await cursor.executemany(
                    """
                    INSERT INTO alert_events
                        (chat_id, message_id, position, status, summary, description, resource, metrics)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )
        return True
