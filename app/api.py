"""Local incident dashboard and JSON API."""

import os
from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path as FilePath
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg import AsyncConnection
from pydantic import BaseModel, StringConstraints
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.storage import initialize
from app.tickets import close_ticket, list_incident_events, list_tickets

WEB = FilePath(__file__).with_name("web")
PAGE_SIZE = 40
DashboardStatus = Literal["open", "closed", "all"]
Resolution = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
IncidentId = Annotated[int, Query(ge=1, le=2**63 - 1)]
PathId = Annotated[int, Path(ge=1, le=2**63 - 1)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with await AsyncConnection.connect(os.environ["DATABASE_URL"]) as connection:
        await initialize(connection)
    yield


async def database():
    async with await AsyncConnection.connect(os.environ["DATABASE_URL"]) as connection:
        yield connection


Database = Annotated[AsyncConnection, Depends(database)]
app = FastAPI(title="Alert Intelligence", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount("/assets", StaticFiles(directory=WEB / "static"), name="assets")
templates = Jinja2Templates(directory=WEB)
templates.env.filters["utc"] = lambda value: value.astimezone(UTC).strftime("%d/%m/%Y · %H:%M UTC")


async def get_incident(connection: AsyncConnection, incident_id: int) -> dict:
    incidents = await list_tickets(connection, incident_id=incident_id)
    if not incidents:
        raise HTTPException(404, "Incidente inesistente")
    return {**incidents[0], "events": await list_incident_events(connection, incident_id)}


def dashboard_url(status: str, search: str, *, page: int = 1, incident: int | None = None) -> str:
    query = {"status": status, "search": search, "page": page}
    if incident is not None:
        query["incident"] = incident
    return "/?" + urlencode(query)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request,
    connection: Database,
    status: DashboardStatus = "open",
    search: Annotated[str, Query(max_length=120)] = "",
    page: Annotated[int, Query(ge=1, le=1000000)] = 1,
    incident: IncidentId | None = None,
):
    search = search.strip()
    rows = await list_tickets(
        connection, status=None if status == "all" else status, search=search,
        limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE,
    )
    visible = rows[:PAGE_SIZE]
    selected_id = incident if incident is not None else (visible[0]["incident_id"] if visible else None)
    selected = await get_incident(connection, selected_id) if selected_id is not None else None
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "tickets": visible, "selected": selected, "status": status, "search": search, "page": page,
            "link": dashboard_url, "next_page": len(rows) > PAGE_SIZE,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/tickets/{ticket_id}/close", include_in_schema=False)
async def close_from_dashboard(
    request: Request, connection: Database, ticket_id: PathId,
    incident_id: Annotated[int, Form(ge=1, le=2**63 - 1)],
    resolution: Annotated[Resolution, Form()],
):
    if request.headers.get("origin") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Origine della richiesta non valida")
    selected = await list_tickets(connection, incident_id=incident_id)
    if not selected or selected[0]["ticket_id"] != ticket_id:
        raise HTTPException(404, "Ticket inesistente")
    await finish_ticket(connection, ticket_id, resolution)
    return RedirectResponse(dashboard_url("all", "", incident=incident_id) + "#incident", status_code=303)


class TicketClosure(BaseModel):
    resolution: Resolution


@app.get("/api/incidents")
async def incidents(
    connection: Database,
    status: DashboardStatus = "all",
    search: Annotated[str, Query(max_length=120)] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
):
    return await list_tickets(
        connection, status=None if status == "all" else status,
        search=search.strip(), limit=limit, offset=offset,
    )


@app.get("/api/incidents/{incident_id}")
async def incident_detail(connection: Database, incident_id: PathId):
    return await get_incident(connection, incident_id)


async def finish_ticket(connection: AsyncConnection, ticket_id: int, resolution: str):
    if not await close_ticket(connection, ticket_id, resolution):
        raise HTTPException(409, "Ticket inesistente o già chiuso")


@app.post("/api/tickets/{ticket_id}/close", status_code=204)
async def close_from_api(request: Request, connection: Database, ticket_id: PathId, closure: TicketClosure):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "Usa application/json")
    await finish_ticket(connection, ticket_id, closure.resolution)
