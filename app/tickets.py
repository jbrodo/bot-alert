"""Command-line operations for incident tickets and reviewed correlations."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from app.incidents import PROJECT_LOCK, VerifiedMapping, correlate, project_events
from app.storage import initialize


async def close_ticket(connection: AsyncConnection, ticket_id: int, resolution: str) -> bool:
    resolution = resolution.strip()
    if not resolution:
        raise ValueError("Serve una nota di chiusura")

    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (PROJECT_LOCK,))
        updated = await connection.execute(
            """
            UPDATE incidents i SET closed_at = GREATEST(clock_timestamp(), i.opened_at)
            FROM tickets t
            WHERE t.id = %s AND i.id = t.incident_id AND i.closed_at IS NULL
            """,
            (ticket_id,),
        )
        if updated.rowcount != 1:
            return False
        await connection.execute(
            "UPDATE tickets SET resolution = %s WHERE id = %s", (resolution, ticket_id)
        )
        return True


async def list_tickets(
    connection: AsyncConnection,
    *,
    incident_id: int | None = None,
    status: str | None = None,
    search: str = "",
    limit: int | None = None,
    offset: int = 0,
    include_latest: bool = False,
) -> list[dict]:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT t.id AS ticket_id, i.id AS incident_id,
                   CASE WHEN i.closed_at IS NULL THEN 'OPEN' ELSE 'CLOSED' END AS status,
                   i.alertname, i.resource_key,
                   i.opened_at, i.closed_at, t.resolution, events.event_count,
                   COALESCE(links.correlations, '[]'::jsonb) AS correlations,
                   CASE WHEN %(include_latest)s THEN (
                       SELECT max(n.sent_at)
                       FROM incident_events e
                       JOIN telegram_notifications n USING (chat_id, message_id)
                       WHERE e.incident_id = i.id
                   ) END AS latest_event_at
            FROM tickets t
            JOIN incidents i ON i.id = t.incident_id
            CROSS JOIN LATERAL (
                SELECT count(*) AS event_count FROM incident_events WHERE incident_id = i.id
            ) events
            CROSS JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'incident_id', other.incident_id, 'evidence', c.evidence
                ) ORDER BY other.incident_id) AS correlations
                FROM incident_correlations c
                CROSS JOIN LATERAL (
                    SELECT CASE WHEN c.first_incident_id = i.id THEN c.second_incident_id
                                ELSE c.first_incident_id END AS incident_id
                ) other
                WHERE i.id IN (c.first_incident_id, c.second_incident_id)
            ) links
            WHERE (%(incident_id)s::bigint IS NULL OR i.id = %(incident_id)s)
              AND (%(status)s::text IS NULL
                   OR (i.closed_at IS NULL AND %(status)s = 'open')
                   OR (i.closed_at IS NOT NULL AND %(status)s = 'closed'))
              AND (%(search)s = '' OR strpos(lower(i.alertname), lower(%(search)s)) > 0
                   OR strpos(lower(i.resource_key::text), lower(%(search)s)) > 0)
            ORDER BY CASE WHEN %(limit)s::integer IS NOT NULL THEN t.id END DESC, t.id
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            dict(incident_id=incident_id, status=status, search=search, limit=limit,
                 offset=offset, include_latest=include_latest),
        )
        rows = await cursor.fetchall()
        if not include_latest:
            for row in rows:
                del row["latest_event_at"]
        return rows


async def list_incident_events(connection: AsyncConnection, incident_id: int) -> list[dict]:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT n.sent_at, n.severity, n.raw_text, e.status, e.summary, e.description,
                   e.resource, e.metrics, e.message_id, e.position
            FROM incident_events link
            JOIN alert_events e USING (chat_id, message_id, position)
            JOIN telegram_notifications n USING (chat_id, message_id)
            WHERE link.incident_id = %s
            ORDER BY n.sent_at DESC, e.message_id DESC, e.position
            """,
            (incident_id,),
        )
        return await cursor.fetchall()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gestione ticket di Alert Intelligence")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Elenca i ticket")
    commands.add_parser("backfill", help="Elabora gli eventi già salvati")
    close = commands.add_parser("close", help="Chiudi un ticket e il relativo incidente")
    close.add_argument("ticket_id", type=int)
    close.add_argument("--resolution", required=True)
    link = commands.add_parser("correlate", help="Collega due incidenti con una mappatura verificata")
    link.add_argument("first_incident_id", type=int)
    link.add_argument("second_incident_id", type=int)
    link.add_argument("--mapping", required=True, type=Path)
    return parser.parse_args()


async def _run(args: argparse.Namespace, database_url: str) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        await initialize(connection)
        if args.command == "list":
            print(json.dumps(await list_tickets(connection), default=str, ensure_ascii=False, indent=2))
        elif args.command == "close":
            if not await close_ticket(connection, args.ticket_id, args.resolution):
                raise ValueError("Ticket inesistente o già chiuso")
            print("Ticket chiuso")
        elif args.command == "backfill":
            print(f"Eventi collegati: {await project_events(connection)}")
        elif args.command == "correlate":
            mapping = VerifiedMapping.model_validate_json(args.mapping.read_text(encoding="utf-8"))
            if await correlate(connection, args.first_incident_id, args.second_incident_id, mapping):
                print("Correlazione registrata")
            else:
                print("Correlazione già presente")


def main() -> None:
    args = _arguments()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Imposta DATABASE_URL")
    try:
        asyncio.run(_run(args, database_url))
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
