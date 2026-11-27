import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql

from app.parser.telegram import parse_telegram_message
from app.storage import initialize, record_notification
from app.telegram import manager
from app.telegram.laya import Decision


def test_manager_reports_once_and_closes_only_latest_resolved(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires TEST_DATABASE_URL with permission to create a schema")

    decision = Decision(
        choice="verify_recovery",
        probabilities={"investigate": 0.1, "verify_recovery": 0.8, "review": 0.1},
        confidence=0.7,
    )
    monkeypatch.setattr(manager, "decide", AsyncMock(return_value=decision))
    bot = AsyncMock()
    text = "LonghornVolumeQuasiPieno\nGravità: warning\n• Volume Longhorn pvc-demo quasi pieno\nSpazio usato al 90%."

    async def run():
        async with await AsyncConnection.connect(dsn) as connection:
            async with connection.transaction(force_rollback=True):
                schema = sql.Identifier(f"manager_test_{uuid4().hex}")
                await connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
                await connection.execute(
                    sql.SQL("SET LOCAL search_path TO {}").format(schema)
                )
                await initialize(connection)
                now = datetime.now(UTC)
                for number, prefix in [(1, "🔥 "), (2, "✅ RISOLTO: "), (3, "🔥 ")]:
                    message = prefix + text
                    await record_notification(
                        connection,
                        chat_id=-10,
                        message_id=number,
                        sender_id=11,
                        sent_at=now + timedelta(seconds=number),
                        text=message,
                        parsed=parse_telegram_message(message),
                    )
                await manager.initialize_chat(connection, -10)
                await manager.initialize_chat(connection, -10)
                assert await manager.process_next(connection, bot, -10)
                assert not await manager.process_next(connection, bot, -10)
                cursor = await connection.execute("SELECT closed_at FROM incidents")
                incident_state = await cursor.fetchone()
                assert incident_state is not None
                assert incident_state[0] is None
                assert bot.send_message.await_count == 1

                message = "✅ RISOLTO: " + text
                await record_notification(
                    connection,
                    chat_id=-10,
                    message_id=4,
                    sender_id=11,
                    sent_at=now + timedelta(seconds=4),
                    text=message,
                    parsed=parse_telegram_message(message),
                )
                bot.send_message.side_effect = RuntimeError("invio non riuscito")
                with pytest.raises(RuntimeError, match="invio non riuscito"):
                    await manager.process_next(connection, bot, -10)
                cursor = await connection.execute("SELECT closed_at FROM incidents")
                incident_state = await cursor.fetchone()
                assert incident_state is not None
                assert incident_state[0] is None
                bot.send_message.side_effect = None
                assert await manager.process_next(connection, bot, -10)
                assert not await manager.process_next(connection, bot, -10)
                cursor = await connection.execute(
                    "SELECT closed_at, resolution FROM incidents JOIN tickets ON incidents.id = tickets.incident_id"
                )
                result = await cursor.fetchone()
                assert result is not None
                closed, note = result
                assert closed is not None
                assert "#4" in note
                assert "chiuso automaticamente" in bot.send_message.await_args.args[1]

    asyncio.run(run())
