"""Project Telegram alert events into incidents and tickets."""

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

PROJECT_LOCK = 0x414C455254


def resource_key(alertname: str, resource: dict[str, str], summary: str) -> dict[str, str]:
    if alertname.startswith("LonghornVolume"):
        fields = ("volume",)
    elif alertname == "LonghornDiscoQuasiPieno":
        fields = ("node", "disk")
    elif alertname.startswith("Filesystem"):
        fields = ("instance", "mountpoint")
    elif alertname in {"DiscoIOSaturo", "NodoErroriRete", "NodoPacchettiScartati"}:
        fields = ("instance", "device")
    elif alertname == "LonghornMetricheAssenti":
        return {"scope": "monitoring_source"}
    else:
        fields = ("node",) if resource.get("node") else ("instance",)

    if not all(resource.get(field) for field in fields):
        return {}
    key = {field: resource[field] for field in fields}
    if alertname == "NodoSottoPressione":
        key["condition"] = summary.rpartition(" in ")[2]
    return key


async def project_events(
    connection: AsyncConnection, notification: tuple[int, int] | None = None
) -> int:
    """Attach known-status events and open one ticket per active alert identity."""
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (PROJECT_LOCK,))
        chat_id, message_id = notification or (None, None)
        pending = await connection.execute(
            """
            SELECT e.chat_id, e.message_id, e.position, n.sender_id, n.sent_at,
                   n.alertname, e.status, e.resource, e.summary
            FROM alert_events e
            JOIN telegram_notifications n USING (chat_id, message_id)
            LEFT JOIN incident_events linked USING (chat_id, message_id, position)
            WHERE linked.incident_id IS NULL AND e.status IN ('FIRING', 'RESOLVED')
              AND n.alertname IS NOT NULL
              AND (%s::bigint IS NULL OR (e.chat_id, e.message_id) = (%s, %s))
            ORDER BY n.sent_at, e.chat_id, e.message_id, e.position
            """,
            (chat_id, chat_id, message_id),
        )
        linked_count = 0
        for row in await pending.fetchall():
            chat_id, message_id, position, sender_id, sent_at, alertname, status, resource, summary = row
            key = resource_key(alertname, resource, summary)
            if not key and status == "RESOLVED":
                continue

            identity = (
                chat_id, sender_id, alertname,
                Jsonb(key or {"message_id": message_id, "position": position}),
            )
            cursor = await connection.execute(
                """
                SELECT id FROM incidents
                WHERE (chat_id, sender_id, alertname, resource_key) = (%s, %s, %s, %s)
                  AND opened_at <= %s AND (closed_at IS NULL OR closed_at > %s)
                ORDER BY opened_at DESC, id DESC LIMIT 1
                """,
                (*identity, sent_at, sent_at),
            )
            incident = await cursor.fetchone()
            if incident is None:
                if status == "RESOLVED":
                    continue
                cursor = await connection.execute(
                    """
                    INSERT INTO incidents (chat_id, sender_id, alertname, resource_key, opened_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (chat_id, sender_id, alertname, resource_key) WHERE closed_at IS NULL
                    DO UPDATE SET opened_at = LEAST(incidents.opened_at, excluded.opened_at)
                    RETURNING id
                    """,
                    (*identity, sent_at),
                )
                incident = await cursor.fetchone()
                await connection.execute(
                    "INSERT INTO tickets (incident_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    incident,
                )
            await connection.execute(
                """
                INSERT INTO incident_events (chat_id, message_id, position, incident_id)
                VALUES (%s, %s, %s, %s)
                """,
                (chat_id, message_id, position, incident[0]),
            )
            linked_count += 1
        return linked_count


class VerifiedMapping(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    node: str = Field(min_length=1)
    device: str = Field(min_length=1)
    volume: str = Field(min_length=1)
    source: str = Field(min_length=1)
    xfs_instance: str | None = None
    kubernetes_instance: str | None = None
    mountpoint: str | None = None
    longhorn_disk: str | None = None


def _family(alertname: str, key: dict[str, str]) -> str:
    if alertname.startswith("XFS"):
        return "XFS"
    if alertname.startswith("Longhorn"):
        return "Longhorn"
    if "node" in key or "mountpoint" in key or alertname in {"DiscoIOSaturo", "NodoFilesystemReadOnlyKernel"}:
        return "Kubernetes"
    raise ValueError("L'alert non identifica una risorsa di storage o un nodo Kubernetes")


def _matches_mapping(family: str, key: dict[str, str], mapping: VerifiedMapping) -> bool:
    if family == "XFS":
        return bool(key.get("instance")) and key["instance"] == mapping.xfs_instance
    expected = (
        {"node": mapping.node, "volume": mapping.volume, "disk": mapping.longhorn_disk}
        if family == "Longhorn"
        else {
            "node": mapping.node,
            "instance": mapping.kubernetes_instance,
            "device": mapping.device,
            "mountpoint": mapping.mountpoint,
        }
    )
    identifiers = key.keys() & expected.keys()
    return bool(identifiers) and all(key[field] == expected[field] for field in identifiers)


async def correlate(
    connection: AsyncConnection, first: int, second: int, mapping: VerifiedMapping
) -> bool:
    """Record an association supported by an operator-verified mapping."""
    if first == second:
        raise ValueError("Servono due incidenti distinti")
    async with connection.transaction():
        cursor = await connection.execute(
            "SELECT chat_id, sender_id, alertname, resource_key FROM incidents WHERE id IN (%s, %s)",
            (first, second),
        )
        incidents = await cursor.fetchall()
        if len(incidents) != 2:
            raise ValueError("Uno degli incidenti non esiste")
        chat, sender, first_name, first_key = incidents[0]
        other_chat, other_sender, second_name, second_key = incidents[1]
        if (chat, sender) != (other_chat, other_sender):
            raise ValueError("Gli incidenti appartengono a fonti di monitoring diverse")
        first_family = _family(first_name, first_key)
        second_family = _family(second_name, second_key)
        if first_family == second_family:
            raise ValueError("Il collegamento richiede due famiglie di alert distinte")
        if not (
            _matches_mapping(first_family, first_key, mapping)
            and _matches_mapping(second_family, second_key, mapping)
        ):
            raise ValueError("Gli identificativi degli incidenti non coincidono con la mappatura")
        left, right = sorted((first, second))
        cursor = await connection.execute(
            """
            INSERT INTO incident_correlations (first_incident_id, second_incident_id, evidence)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (left, right, Jsonb(mapping.model_dump(exclude_none=True))),
        )
        return cursor.rowcount == 1
