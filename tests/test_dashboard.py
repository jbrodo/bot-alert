import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from app import api
from app.parser.telegram import parse_telegram_message
from app.storage import initialize, record_notification


def test_dashboard_and_api_with_postgres(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires TEST_DATABASE_URL with permission to create a schema")
    schema = f"dashboard_test_{uuid4().hex}"
    database_url = make_conninfo(dsn, options=f"-csearch_path={schema}")
    monkeypatch.setenv("DATABASE_URL", database_url)
    message = (
        "🔥 LonghornVolumeQuasiPieno\nGravità: warning\n"
        "• Volume Longhorn pvc-abc quasi pieno\nSpazio usato al 136%. <script>alert(1)</script>"
    )

    async def prepare():
        async with await AsyncConnection.connect(dsn) as connection:
            await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        async with await AsyncConnection.connect(database_url) as connection:
            await initialize(connection)
            await record_notification(
                connection, chat_id=-10, sender_id=11, message_id=21, sent_at=datetime.now(UTC),
                text=message, parsed=parse_telegram_message(message),
            )

    async def cleanup():
        async with await AsyncConnection.connect(dsn) as connection:
            await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    async def run():
        try:
            await prepare()
            async with api.lifespan(api.app), AsyncClient(
                transport=ASGITransport(app=api.app), base_url="http://localhost", follow_redirects=True,
            ) as client:
                page = await client.get("/")
                assert page.status_code == 200
                assert page.headers["cache-control"] == "no-store"
                assert "pvc-abc" in page.text and "136%" in page.text
                assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
                assert "<script>alert(1)</script>" not in page.text
                assert (await client.get("/assets/dashboard.css")).status_code == 200
                assert (await client.get("/", headers={"host": "unexpected.example"})).status_code == 400
                assert (await client.get("/api/incidents/9999")).status_code == 404
                assert (await client.get("/api/incidents/9999999999999999999999")).status_code == 422
                assert (await client.get("/api/incidents", params={"search": "%"})).json() == []
                incidents = await client.get("/api/incidents", params={"search": "PVC-ABC"})
                incident = incidents.json()[0]
                incident_id, ticket_id = incident["incident_id"], incident["ticket_id"]
                detail = (await client.get(f"/api/incidents/{incident_id}")).json()
                assert detail["events"][0]["raw_text"] == message
                assert detail["events"][0]["metrics"][0]["raw"] == "136%"
                assert detail["status"] == "OPEN"

                form_path = f"/tickets/{ticket_id}/close"
                api_path = f"/api/tickets/{ticket_id}/close"
                form = {"incident_id": incident_id, "resolution": "Presidio verificato"}
                foreign = await client.post(form_path, data=form, headers={"origin": "https://other.example"})
                assert foreign.status_code == 403
                assert (await client.post(api_path, json={"resolution": " "})).status_code == 422
                closed = await client.post(form_path, data=form, headers={"origin": "http://localhost"})
                assert closed.history[0].status_code == 303
                assert "Presidio verificato" in closed.text
                assert (await client.post(api_path, json={"resolution": "Doppio invio"})).status_code == 409
                assert (await client.get("/api/incidents", params={"status": "open"})).json() == []
                closed_incidents = (await client.get("/api/incidents", params={"status": "closed"})).json()
                assert closed_incidents[0]["status"] == "CLOSED"
        finally:
            await cleanup()

    asyncio.run(run())
