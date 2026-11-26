"""Locally generated, event-referenced incident summaries."""

import json
import os
from datetime import UTC

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.tickets import list_incident_events


class SummaryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    event_refs: list[str] = Field(min_length=1, max_length=10)


class IncidentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeline: list[SummaryPoint] = Field(max_length=8)
    findings: list[SummaryPoint] = Field(max_length=8)
    to_verify: list[SummaryPoint] = Field(max_length=8)
    included_events: int = Field(ge=0)
    total_events: int = Field(ge=0)


class SummaryUnavailable(Exception):
    pass


async def summarize_incident(connection, incident_id: int) -> IncidentSummary:
    base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    model = os.getenv("LOCAL_LLM_MODEL", "gemma3:4b").strip()
    if not model:
        raise SummaryUnavailable(
            "Configura LOCAL_LLM_MODEL con il nome del modello locale."
        )

    events = await list_incident_events(connection, incident_id)
    selected = events[:40]
    allowed_refs = {f"{event['message_id']}:{event['position']}" for event in selected}
    payload = [
        {
            "ref": f"{event['message_id']}:{event['position']}",
            "sent_at": event["sent_at"].astimezone(UTC).isoformat(),
            "status": event["status"],
            "severity": event["severity"],
            "summary": event["summary"],
            "description": event["description"],
            "resource": event["resource"],
            "metrics": event["metrics"],
        }
        for event in selected
    ]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=3)) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {os.getenv('LOCAL_LLM_API_KEY', 'ollama')}"
                },
                json={
                    "model": model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Scrivi in italiano un riepilogo conciso basato soltanto sugli eventi forniti. "
                                "Restituisci JSON con timeline, findings, to_verify, included_events, total_events. "
                                "Ogni elemento delle tre liste contiene text ed event_refs, con riferimenti ref "
                                "copiati esattamente dagli eventi. Non inventare fatti. In to_verify segnala "
                                "le informazioni mancanti o gli stati indeterminati. Mantieni entrambi i conteggi."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(payload, ensure_ascii=False),
                        },
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            summary = IncidentSummary.model_validate_json(content)
    except (
        httpx.HTTPError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        ValidationError,
    ) as exc:
        raise SummaryUnavailable(
            "Il modello locale non ha restituito un riepilogo valido. Verifica il servizio e riprova."
        ) from exc

    expected_counts = (len(selected), len(events))
    if (summary.included_events, summary.total_events) != expected_counts:
        raise SummaryUnavailable(
            "Il modello ha restituito conteggi degli eventi non validi."
        )
    for point in (*summary.timeline, *summary.findings, *summary.to_verify):
        if not set(point.event_refs) <= allowed_refs:
            raise SummaryUnavailable(
                "Il riepilogo contiene riferimenti a eventi inesistenti."
            )
    return summary
