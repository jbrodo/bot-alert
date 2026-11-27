import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql

from app import storage
from app.incidents import VerifiedMapping, correlate, project_events, resource_key
from app.parser.telegram import parse_telegram_message
from app.tickets import close_ticket, list_recurrences, list_tickets


@pytest.mark.parametrize(
    ("name", "resource", "summary", "expected"),
    [
        (
            "LonghornVolumeDegradato",
            {"volume": "vol-1", "pvc": "claim"},
            "",
            {"volume": "vol-1"},
        ),
        ("LonghornVolumeFaulted", {"pvc": "claim"}, "", {}),
        ("FilesystemSolaLettura", {"instance": "node:9100"}, "", {}),
        (
            "NodoSottoPressione",
            {"node": "node"},
            "Nodo node in DiskPressure",
            {"node": "node", "condition": "DiskPressure"},
        ),
        (
            "XFSFilesystemSpento",
            {"instance": "npd:20257"},
            "",
            {"instance": "npd:20257"},
        ),
    ],
)
def test_alert_identity_keeps_the_fields_required_for_its_resource(
    name, resource, summary, expected
):
    assert resource_key(name, resource, summary) == expected


def test_missing_incident_insert_result_fails_explicitly():
    connection = AsyncMock(spec=AsyncConnection)
    pending = AsyncMock()
    pending.fetchall.return_value = [
        (
            -10,
            1,
            0,
            20,
            datetime.now(UTC),
            "LonghornVolumeFaulted",
            "FIRING",
            {"volume": "vol-1"},
            "",
        )
    ]
    lookup = AsyncMock()
    lookup.fetchone.return_value = None
    insert = AsyncMock()
    insert.fetchone.return_value = None
    connection.execute.side_effect = [None, pending, lookup, insert]

    with pytest.raises(RuntimeError, match="Incident insert did not return an ID"):
        asyncio.run(project_events(connection))


def test_incident_and_ticket_lifecycle(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires TEST_DATABASE_URL with permission to create a schema")

    longhorn = "Volume Longhorn vol-1 quasi pieno\nSpazio usato al 136%."
    xfs = "Filesystem XFS spento dal kernel su npd:20257\nXFS è andato in shutdown."
    kubernetes = (
        "Filesystem in sola lettura su node:9100\n/mnt/data è montato read-only."
    )
    start = datetime.now(UTC) - timedelta(minutes=10)

    async def run():
        async with await AsyncConnection.connect(dsn) as connection:
            async with connection.transaction(force_rollback=True):
                schema = sql.Identifier(f"incidents_test_{uuid4().hex}")
                await connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
                await connection.execute(
                    sql.SQL("SET LOCAL search_path TO {}").format(schema)
                )
                await storage.initialize(connection)
                # A second startup must keep the same schema.
                await storage.initialize(connection)

                async def notify(
                    message_id, name, entries, *, resolved=False, sent_at=None
                ):
                    header = "✅ RISOLTO:" if resolved else "🔥"
                    message = f"{header} {name}\nGravità: critical\n• {entries}"
                    return await storage.record_notification(
                        connection,
                        chat_id=-10,
                        message_id=message_id,
                        sender_id=20,
                        sent_at=sent_at or start + timedelta(seconds=message_id),
                        text=message,
                        parsed=parse_telegram_message(message),
                    )

                # Messages retained by the previous collector are projected on demand.
                with monkeypatch.context() as patch:
                    patch.setattr(storage, "project_events", AsyncMock())
                    await notify(1, "LonghornVolumeQuasiPieno", longhorn)
                assert await list_tickets(connection) == []
                assert await project_events(connection) == 1
                assert await project_events(connection) == 0

                assert await notify(2, "LonghornVolumeQuasiPieno", longhorn)
                assert not await notify(2, "LonghornVolumeQuasiPieno", longhorn)
                await notify(3, "XFSFilesystemSpento", xfs)
                await notify(4, "FilesystemSolaLettura", kubernetes)
                await notify(5, "LonghornVolumeQuasiPieno", longhorn, resolved=True)
                await notify(6, "XFSFilesystemSpento", xfs, resolved=True)
                await notify(
                    7,
                    "LonghornVolumeQuasiPieno",
                    longhorn.replace("vol-1", "vol-2"),
                    resolved=True,
                )
                await notify(
                    8,
                    "LonghornVolumeQuasiPieno",
                    f"{longhorn}\n• {longhorn.replace('vol-1', 'vol-3')}",
                )

                longhorn_ticket, xfs_ticket, kubernetes_ticket = await list_tickets(
                    connection
                )
                assert [
                    ticket["event_count"]
                    for ticket in (longhorn_ticket, xfs_ticket, kubernetes_ticket)
                ] == [
                    3,
                    2,
                    1,
                ]
                assert all(
                    ticket["closed_at"] is None
                    for ticket in (longhorn_ticket, xfs_ticket, kubernetes_ticket)
                )
                assert xfs_ticket["correlations"] == []

                mapping = VerifiedMapping(
                    node="worker-1",
                    device="dm-0",
                    volume="vol-1",
                    source="Verifica di device e volume per questo evento kernel",
                    xfs_instance="npd:20257",
                    kubernetes_instance="node:9100",
                    mountpoint="/mnt/data",
                )
                lh_id = longhorn_ticket["incident_id"]
                xfs_id = xfs_ticket["incident_id"]
                kube_id = kubernetes_ticket["incident_id"]
                with pytest.raises(ValueError, match="non coincidono"):
                    await correlate(
                        connection,
                        lh_id,
                        xfs_id,
                        mapping.model_copy(update={"volume": "vol-2"}),
                    )
                assert await correlate(connection, lh_id, xfs_id, mapping)
                assert not await correlate(connection, xfs_id, lh_id, mapping)
                assert await correlate(connection, xfs_id, kube_id, mapping)

                ticket_id = xfs_ticket["ticket_id"]
                with pytest.raises(ValueError, match="nota di chiusura"):
                    await close_ticket(connection, ticket_id, " ")
                assert await close_ticket(
                    connection, ticket_id, "Ripristino verificato sul device"
                )
                assert not await close_ticket(connection, ticket_id, "Sovrascrittura")
                closed = (await list_tickets(connection))[1]
                assert closed["resolution"] == "Ripristino verificato sul device"
                assert [link["incident_id"] for link in closed["correlations"]] == [
                    lh_id,
                    kube_id,
                ]
                assert closed["correlations"][0]["evidence"]["device"] == "dm-0"
                await notify(
                    9,
                    "XFSFilesystemSpento",
                    xfs,
                    sent_at=closed["closed_at"] + timedelta(seconds=1),
                )
                tickets = await list_tickets(connection)
                assert len(tickets) == 4
                assert tickets[-1]["closed_at"] is None
                assert tickets[-1]["incident_id"] != xfs_id
                (previous,) = await list_recurrences(
                    connection, tickets[-1]["incident_id"]
                )
                assert previous["incident_id"] == xfs_id
                assert previous["notification_count"] == 2
                assert previous["resolution"] == "Ripristino verificato sul device"
                assert await list_recurrences(connection, lh_id) == []

                saved = await connection.execute("SELECT count(*) FROM alert_events")
                assert await saved.fetchone() == (10,)

    asyncio.run(run())
