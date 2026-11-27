import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from app import summaries


def test_summary_checks_event_references_and_counts(monkeypatch):
    events = [
        {
            "message_id": number,
            "position": 0,
            "sent_at": datetime(2026, 11, 27, tzinfo=UTC),
            "status": None,
            "severity": "warning",
            "summary": "Alert",
            "description": "",
            "resource": {},
            "metrics": [],
        }
        for number in range(41)
    ]
    reference = "0:0"

    async def load_events(connection, incident_id):
        return events

    def respond(request):
        content = {
            "timeline": [{"text": "Stato indeterminato", "event_refs": [reference]}],
            "findings": [],
            "to_verify": [],
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(summaries, "list_incident_events", load_events)
    monkeypatch.setattr(
        summaries.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond)),
    )
    result = asyncio.run(summaries.summarize_incident(None, 1))
    assert (result.included_events, result.total_events) == (40, 41)
    assert result.timeline[0].event_refs == ["0:0"]

    reference = "40:0"
    with pytest.raises(summaries.SummaryUnavailable, match="riferimenti"):
        asyncio.run(summaries.summarize_incident(None, 1))
