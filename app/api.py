"""Local incident dashboard and JSON API."""

import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from itertools import groupby
from pathlib import Path as FilePath
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg import AsyncConnection
from pydantic import BaseModel, StringConstraints, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.incidents import VerifiedMapping, correlate
from app.storage import initialize, list_notifications
from app.tickets import close_ticket, list_incident_events, list_tickets

WEB = FilePath(__file__).with_name("web")
PAGE_SIZE = 40
DashboardStatus = Literal["open", "closed", "all"]
DashboardSearch = Annotated[str, Query(max_length=120)]
DashboardPage = Annotated[int, Query(ge=1, le=1000000)]
NotificationScope = Literal["all", "unparsed", "unlinked"]
NotificationSearch = Annotated[str, Query(max_length=120)]
Resolution = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
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
templates.env.filters["utc"] = lambda value: value.astimezone(UTC).strftime(
    "%d/%m/%Y · %H:%M UTC"
)


async def get_incident(connection: AsyncConnection, incident_id: int) -> dict:
    incidents = await list_tickets(connection, incident_id=incident_id)
    if not incidents:
        raise HTTPException(404, "Incidente inesistente")
    return {
        **incidents[0],
        "events": await list_incident_events(connection, incident_id),
    }


def dashboard_url(
    status: str, search: str, *, page: int = 1, incident: int | None = None
) -> str:
    query = {"status": status, "search": search, "page": page}
    if incident is not None:
        query["incident"] = incident
    return "/?" + urlencode(query)


def notification_url(
    scope: str,
    search: str,
    since: date | None,
    until: date | None,
    page: int = 1,
) -> str:
    query = {"scope": scope, "search": search, "page": page}
    if since is not None:
        query["since"] = since.isoformat()
    if until is not None:
        query["until"] = until.isoformat()
    return "/notifications?" + urlencode(query)


async def render_dashboard(
    request: Request,
    connection: AsyncConnection,
    status: DashboardStatus,
    search: str,
    page: int,
    incident: int | None,
    *,
    form_error: str = "",
    draft: str = "",
    correlation_values: dict[str, str] | None = None,
    correlation_error: str = "",
    correlation_invalid: tuple[str, ...] = (),
    response_status: int = 200,
):
    search = search.strip()
    rows = await list_tickets(
        connection,
        status=None if status == "all" else status,
        search=search,
        limit=PAGE_SIZE + 1,
        offset=(page - 1) * PAGE_SIZE,
        include_latest=True,
    )
    visible = rows[:PAGE_SIZE]
    selected_id = (
        incident
        if incident is not None
        else (visible[0]["incident_id"] if visible else None)
    )
    selected = (
        await get_incident(connection, selected_id) if selected_id is not None else None
    )
    event_groups = []
    if selected:
        event_fields = (
            "status",
            "severity",
            "summary",
            "description",
            "resource",
            "metrics",
            "raw_text",
        )
        event_groups = [
            list(events)
            for _, events in groupby(
                selected["events"],
                key=lambda event: tuple(event[field] for field in event_fields),
            )
        ]
    selected_in_queue = any(ticket["incident_id"] == selected_id for ticket in visible)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "tickets": visible,
            "selected": selected,
            "status": status,
            "search": search,
            "page": page,
            "link": dashboard_url,
            "next_page": len(rows) > PAGE_SIZE,
            "event_groups": event_groups,
            "selected_in_queue": selected_in_queue,
            "form_error": form_error,
            "draft": draft,
            "correlation_values": correlation_values or {},
            "correlation_error": correlation_error,
            "correlation_invalid": correlation_invalid,
        },
        status_code=response_status,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request,
    connection: Database,
    status: DashboardStatus = "open",
    search: DashboardSearch = "",
    page: DashboardPage = 1,
    incident: IncidentId | None = None,
):
    return await render_dashboard(request, connection, status, search, page, incident)


def require_same_origin(request: Request) -> None:
    if request.headers.get("origin") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Origine della richiesta non valida")


@app.post("/incidents/{incident_id}/correlate", include_in_schema=False)
async def correlate_from_dashboard(
    request: Request,
    connection: Database,
    incident_id: PathId,
    status: DashboardStatus = "open",
    search: DashboardSearch = "",
    page: DashboardPage = 1,
):
    require_same_origin(request)
    async with request.form() as form:
        values = {
            name: value
            for name in ("other_incident", *VerifiedMapping.model_fields)
            if isinstance(value := form.get(name), str)
        }
    other = values.get("other_incident", "").strip()
    error = ""
    invalid = ()
    if not (
        other.isascii()
        and other.isdecimal()
        and len(other) <= 19
        and 1 <= int(other) <= 2**63 - 1
    ):
        error = "Inserisci un ID incidente valido."
        invalid = ("other_incident",)
    else:
        try:
            mapping = VerifiedMapping.model_validate(
                {
                    name: values[name].strip()
                    for name in VerifiedMapping.model_fields
                    if name in values and values[name].strip()
                }
            )
            await correlate(connection, incident_id, int(other), mapping)
        except ValidationError as exc:
            error = "Compila i campi obbligatori della mappatura."
            invalid = tuple(str(issue["loc"][0]) for issue in exc.errors())
        except ValueError as exc:
            error = str(exc)

    if error:
        return await render_dashboard(
            request,
            connection,
            status,
            search,
            page,
            incident_id,
            correlation_values=values,
            correlation_error=error,
            correlation_invalid=invalid,
            response_status=422,
        )
    return RedirectResponse(
        dashboard_url(status, search.strip(), page=page, incident=incident_id)
        + "#correlazioni",
        status_code=303,
    )


@app.post("/tickets/{ticket_id}/close", include_in_schema=False)
async def close_from_dashboard(
    request: Request,
    connection: Database,
    ticket_id: PathId,
    incident_id: Annotated[int, Form(ge=1, le=2**63 - 1)],
    resolution: Annotated[str, Form()] = "",
    status: DashboardStatus = "open",
    search: DashboardSearch = "",
    page: DashboardPage = 1,
):
    require_same_origin(request)
    selected = await list_tickets(connection, incident_id=incident_id)
    if not selected or selected[0]["ticket_id"] != ticket_id:
        raise HTTPException(404, "Ticket inesistente")
    note = resolution.strip()
    if selected[0]["status"] == "OPEN" and not 1 <= len(note) <= 2000:
        return await render_dashboard(
            request,
            connection,
            status,
            search,
            page,
            incident_id,
            draft=resolution,
            form_error="Inserisci una nota di chiusura da 1 a 2000 caratteri.",
            response_status=422,
        )
    if selected[0]["status"] == "CLOSED" or not await close_ticket(
        connection, ticket_id, note
    ):
        return await render_dashboard(
            request,
            connection,
            status,
            search,
            page,
            incident_id,
            draft=resolution,
            form_error="Il ticket è già stato chiuso. Consulta la nota salvata.",
            response_status=409,
        )
    return RedirectResponse(
        dashboard_url(status, search.strip(), page=page, incident=incident_id)
        + "#nota-chiusura",
        status_code=303,
    )


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
        connection,
        status=None if status == "all" else status,
        search=search.strip(),
        limit=limit,
        offset=offset,
    )


@app.get("/api/incidents/{incident_id}")
async def incident_detail(connection: Database, incident_id: PathId):
    return await get_incident(connection, incident_id)


@app.get("/notifications", response_class=HTMLResponse, include_in_schema=False)
async def notification_registry(
    request: Request,
    connection: Database,
    scope: NotificationScope = "all",
    search: NotificationSearch = "",
    since: date | None = None,
    until: date | None = None,
    page: Annotated[int, Query(ge=1, le=1000000)] = 1,
):
    if since is not None and until is not None and since > until:
        return templates.TemplateResponse(
            request=request,
            name="notifications.html",
            context={
                "messages": [],
                "scope": scope,
                "search": search.strip(),
                "since": since,
                "until": until,
                "page": 1,
                "next_page": False,
                "incident_link": dashboard_url,
                "notification_link": notification_url,
                "filter_error": "L’intervallo di date non è valido.",
            },
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )
    start = datetime.combine(since, time.min, UTC) if since else None
    end = datetime.combine(until + timedelta(days=1), time.min, UTC) if until else None
    search = search.strip()
    messages = await list_notifications(
        connection,
        scope=scope,
        search=search,
        since=start,
        until=end,
        limit=PAGE_SIZE + 1,
        offset=(page - 1) * PAGE_SIZE,
    )
    return templates.TemplateResponse(
        request=request,
        name="notifications.html",
        context={
            "messages": messages[:PAGE_SIZE],
            "scope": scope,
            "search": search,
            "since": since,
            "until": until,
            "page": page,
            "next_page": len(messages) > PAGE_SIZE,
            "incident_link": dashboard_url,
            "notification_link": notification_url,
            "filter_error": "",
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/notifications")
async def notification_records(
    response: Response,
    connection: Database,
    scope: NotificationScope = "all",
    search: Annotated[str, Query(max_length=120)] = "",
    since: date | None = None,
    until: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
):
    response.headers["Cache-Control"] = "no-store"
    if since is not None and until is not None and since > until:
        raise HTTPException(422, "Intervallo di date non valido")
    start = datetime.combine(since, time.min, UTC) if since else None
    end = datetime.combine(until + timedelta(days=1), time.min, UTC) if until else None
    return await list_notifications(
        connection,
        scope=scope,
        search=search.strip(),
        since=start,
        until=end,
        limit=limit,
        offset=offset,
    )


async def finish_ticket(connection: AsyncConnection, ticket_id: int, resolution: str):
    if not await close_ticket(connection, ticket_id, resolution):
        raise HTTPException(409, "Ticket inesistente o già chiuso")


@app.post("/api/tickets/{ticket_id}/close", status_code=204)
async def close_from_api(
    request: Request, connection: Database, ticket_id: PathId, closure: TicketClosure
):
    if (
        request.headers.get("content-type", "").split(";")[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "Usa application/json")
    await finish_ticket(connection, ticket_id, closure.resolution)
