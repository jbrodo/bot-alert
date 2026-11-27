"""Decision support for open monitoring tickets using a local Laya model."""

import os
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

Action = Literal["investigate", "verify_recovery", "review"]


class Decision(BaseModel):
    choice: Action
    probabilities: dict[Action, float]
    confidence: float = Field(ge=0, le=1)


class DecisionUnavailable(Exception):
    pass


async def decide(status: str | None, severity: str | None) -> Decision:
    base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=3)) as client:
            response = await client.post(
                f"{base_url}/systemone",
                json={
                    "model": os.getenv("LAYA_MODEL", "laya:latest"),
                    "state": {
                        "monitoring_status": status or "UNKNOWN",
                        "ticket_status": "OPEN",
                        "severity": severity or "unknown",
                    },
                    "questions": {
                        "action": {
                            "type": "choice",
                            "instructions": "What should the operator do next for this open monitoring ticket?",
                            "criteria": {
                                "investigate": "The monitoring alert is firing and needs investigation.",
                                "verify_recovery": "The monitoring alert resolved and the operator should verify recovery before closing.",
                                "review": "The alert state is uncertain or insufficient to choose.",
                            },
                        }
                    },
                },
            )
            response.raise_for_status()
            decision = Decision.model_validate(response.json()["answers"]["action"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as exc:
        raise DecisionUnavailable(
            "Laya non è disponibile o ha restituito una risposta non valida."
        ) from exc
    if set(decision.probabilities) != {
        "investigate",
        "verify_recovery",
        "review",
    } or any(not 0 <= value <= 1 for value in decision.probabilities.values()):
        raise DecisionUnavailable("Laya ha restituito probabilità non valide.")
    return decision
