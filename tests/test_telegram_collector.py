import asyncio
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.types import Update
from psycopg import AsyncConnection, errors, sql

from app.telegram import collector

CHAT = -1001234567890
SENDER = 123456789
TEXT = (
    "🔥 LonghornVolumeQuasiPieno\nGravità: warning\n\n"
    "• Volume Longhorn pvc-abc quasi pieno\nSpazio usato al 136%.\n\n"
    "• Volume Longhorn pvc-def quasi pieno\nSpazio usato al 118%.\n"
)


def make_update(
    *,
    update_id=20,
    message_id=3,
    chat_id=CHAT,
    sender_id=SENDER,
    is_bot=True,
    text=TEXT,
):
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "date": 1750000000,
                "chat": {"id": chat_id, "type": "supergroup"},
                "from": {"id": sender_id, "is_bot": is_bot, "first_name": "Monitor"},
                "text": text,
            },
        }
    )


def test_accepts_only_text_from_configured_monitor_bot(monkeypatch):
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(collector, "record_notification", record)

    async def run():
        for update in (
            make_update(chat_id=-999),
            make_update(sender_id=SENDER + 1),
            make_update(is_bot=False),
            make_update(text=None),
            make_update(text="formato sconosciuto"),
            make_update(),
        ):
            await collector.handle_update(
                update,
                AsyncMock(spec=AsyncConnection),
                chat_id=CHAT,
                monitor_bot_id=SENDER,
            )

    asyncio.run(run())

    raw, parsed = (call.kwargs for call in record.await_args_list)
    assert raw["text"] == "formato sconosciuto"
    assert raw["parsed"] is None
    assert parsed["text"] == TEXT
    assert [alert.resource.volume for alert in parsed["parsed"].alerts] == [
        "pvc-abc",
        "pvc-def",
    ]


def test_polls_next_offset_after_processing_batch(monkeypatch):
    class EndOfTest(Exception):
        pass

    bot = AsyncMock(spec=Bot)
    bot.get_updates.side_effect = [
        [make_update(update_id=20), make_update(update_id=21)],
        [],
        EndOfTest,
    ]
    handle = AsyncMock()
    monkeypatch.setattr(collector, "handle_update", handle)

    with pytest.raises(EndOfTest):
        asyncio.run(
            collector.poll(
                bot,
                AsyncMock(spec=AsyncConnection),
                chat_id=CHAT,
                monitor_bot_id=SENDER,
            )
        )

    assert [call.kwargs["offset"] for call in bot.get_updates.await_args_list] == [
        None,
        22,
        None,
    ]
    assert [call.args[0].update_id for call in handle.await_args_list] == [20, 21]


def test_storage_failure_stops_polling_before_acknowledgement(monkeypatch):
    bot = AsyncMock(spec=Bot)
    bot.get_updates.return_value = [
        make_update(update_id=20),
        make_update(update_id=21),
    ]
    record = AsyncMock(side_effect=[True, RuntimeError("storage unavailable")])
    monkeypatch.setattr(collector, "record_notification", record)

    with pytest.raises(RuntimeError, match="storage unavailable"):
        asyncio.run(
            collector.poll(
                bot,
                AsyncMock(spec=AsyncConnection),
                chat_id=CHAT,
                monitor_bot_id=SENDER,
            )
        )

    bot.get_updates.assert_awaited_once_with(
        offset=None, timeout=30, allowed_updates=["message"]
    )


def test_postgres_preserves_originals_and_records_events_atomically():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires TEST_DATABASE_URL with permission to create a schema")

    async def run():
        async with await AsyncConnection.connect(dsn) as connection:
            async with connection.transaction(force_rollback=True):
                schema = sql.Identifier(f"collector_test_{uuid4().hex}")
                await connection.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
                await connection.execute(
                    sql.SQL("SET LOCAL search_path TO {}").format(schema)
                )
                await collector.initialize(connection)

                for update in (
                    make_update(message_id=3),
                    make_update(update_id=21, message_id=3),  # Telegram redelivery
                    make_update(
                        update_id=22, message_id=4
                    ),  # New notification with identical text
                    make_update(
                        update_id=23, message_id=5, text="alert originale fuori formato"
                    ),
                ):
                    await collector.handle_update(
                        update, connection, chat_id=CHAT, monitor_bot_id=SENDER
                    )

                # Failing the second event insert rolls back the first event and its parent.
                await connection.execute(
                    "ALTER TABLE alert_events ADD CHECK (message_id <> 6 OR position = 0)"
                )
                with pytest.raises(errors.CheckViolation):
                    await collector.handle_update(
                        make_update(update_id=24, message_id=6),
                        connection,
                        chat_id=CHAT,
                        monitor_bot_id=SENDER,
                    )

                messages = await connection.execute(
                    "SELECT message_id, raw_text, group_status FROM telegram_notifications ORDER BY message_id"
                )
                assert await messages.fetchall() == [
                    (3, TEXT, "FIRING"),
                    (4, TEXT, "FIRING"),
                    (5, "alert originale fuori formato", None),
                ]
                events = await connection.execute(
                    """
                    SELECT message_id, position, status, resource->>'volume', metrics->0->>'raw'
                    FROM alert_events
                    ORDER BY message_id, position
                    """
                )
                assert await events.fetchall() == [
                    (3, 0, None, "pvc-abc", "136%"),
                    (3, 1, None, "pvc-def", "118%"),
                    (4, 0, None, "pvc-abc", "136%"),
                    (4, 1, None, "pvc-def", "118%"),
                ]

    asyncio.run(run())
