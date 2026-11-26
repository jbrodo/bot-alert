"""PostgreSQL storage for Telegram notifications and their alert events."""

from datetime import datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.incidents import PROJECT_LOCK, project_events
from app.parser.telegram import TelegramNotification


async def list_notifications(
    connection: AsyncConnection,
    *,
    scope: str,
    limit: int,
    offset: int,
    search: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT n.chat_id, n.message_id, n.sender_id, n.sent_at, n.alertname, n.severity,
                   n.group_status, n.raw_text, items.event_count, items.linked_count,
                   COALESCE(items.events, '[]'::jsonb) AS events
            FROM telegram_notifications n
            CROSS JOIN LATERAL (
                SELECT count(*) AS event_count, count(link.incident_id) AS linked_count,
                       string_agg(e.resource::text, ' ') AS resources,
                       jsonb_agg(jsonb_build_object(
                           'position', e.position, 'status', e.status, 'summary', e.summary,
                           'incident_id', link.incident_id
                       ) ORDER BY e.position) AS events
                FROM alert_events e
                LEFT JOIN incident_events link USING (chat_id, message_id, position)
                WHERE (e.chat_id, e.message_id) = (n.chat_id, n.message_id)
            ) items
            WHERE (
                %(scope)s = 'all'
                OR (%(scope)s = 'unparsed' AND n.group_status IS NULL)
                OR (%(scope)s = 'unlinked' AND n.group_status IS NOT NULL
                    AND items.linked_count < items.event_count)
            )
              AND (%(search)s = '' OR strpos(lower(
                  concat_ws(' ', n.alertname, n.raw_text, items.resources)
              ), lower(%(search)s)) > 0)
              AND (%(since)s::timestamptz IS NULL OR n.sent_at >= %(since)s)
              AND (%(until)s::timestamptz IS NULL OR n.sent_at < %(until)s)
            ORDER BY n.sent_at DESC, n.chat_id DESC, n.message_id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {
                "scope": scope,
                "search": search,
                "since": since,
                "until": until,
                "limit": limit,
                "offset": offset,
            },
        )
        return await cursor.fetchall()


async def initialize(connection: AsyncConnection) -> None:
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (PROJECT_LOCK,))
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

            CREATE TABLE IF NOT EXISTS incidents (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                chat_id bigint NOT NULL,
                sender_id bigint NOT NULL,
                alertname text NOT NULL,
                resource_key jsonb NOT NULL,
                opened_at timestamptz NOT NULL,
                closed_at timestamptz
            );
            CREATE UNIQUE INDEX IF NOT EXISTS incidents_open_identity
                ON incidents (chat_id, sender_id, alertname, resource_key)
                WHERE closed_at IS NULL;

            CREATE TABLE IF NOT EXISTS tickets (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                incident_id bigint NOT NULL UNIQUE REFERENCES incidents (id),
                created_at timestamptz NOT NULL DEFAULT now(),
                resolution text
            );
            CREATE TABLE IF NOT EXISTS incident_events (
                chat_id bigint NOT NULL,
                message_id bigint NOT NULL,
                position integer NOT NULL,
                incident_id bigint NOT NULL REFERENCES incidents (id),
                PRIMARY KEY (chat_id, message_id, position),
                FOREIGN KEY (chat_id, message_id, position)
                    REFERENCES alert_events (chat_id, message_id, position)
            );
            CREATE INDEX IF NOT EXISTS incident_events_incident_id ON incident_events (incident_id);

            CREATE TABLE IF NOT EXISTS incident_correlations (
                first_incident_id bigint NOT NULL REFERENCES incidents (id),
                second_incident_id bigint NOT NULL REFERENCES incidents (id),
                evidence jsonb NOT NULL,
                PRIMARY KEY (first_incident_id, second_incident_id),
                CHECK (first_incident_id < second_incident_id)
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
                chat_id,
                message_id,
                sender_id,
                sent_at,
                text,
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
                        chat_id,
                        message_id,
                        position,
                        alert.status.value if alert.status else None,
                        alert.summary,
                        alert.description,
                        Jsonb(alert.resource.model_dump(exclude_none=True)),
                        Jsonb(
                            [metric.model_dump(mode="json") for metric in alert.metrics]
                        ),
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
            await project_events(connection, (chat_id, message_id))
        return True
