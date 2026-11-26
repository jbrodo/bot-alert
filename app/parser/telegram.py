"""Parse the plain text Telegram delivers for the monitoring Helm template."""

import re
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class AlertStatus(StrEnum):
    FIRING = "FIRING"
    RESOLVED = "RESOLVED"


class AlertResource(BaseModel):
    instance: str | None = None
    node: str | None = None
    device: str | None = None
    mountpoint: str | None = None
    volume: str | None = None
    pvc: str | None = None
    namespace: str | None = None
    disk: str | None = None


class AlertMetric(BaseModel):
    name: str
    raw: str
    unit: str
    value: Decimal | None


class AlertEntry(BaseModel):
    status: AlertStatus | None
    summary: str
    description: str
    resource: AlertResource
    metrics: tuple[AlertMetric, ...]


class TelegramNotification(BaseModel):
    text: str
    alertname: str
    severity: str | None
    status: AlertStatus
    alerts: tuple[AlertEntry, ...]


_HEADER = re.compile(r"(?:(?P<firing>🔥) |✅ RISOLTO: )(?P<alertname>\S+)")
_BULLET = re.compile(r"(?m)^•[ \t]*")

# These patterns mirror the summaries in doc/raw/values-monitoring.yaml.
_RESOURCE_SUMMARIES = {
    "NodoCPUAlta": r"CPU alta su (?P<instance>.+)",
    "NodoCPUCritica": r"CPU critica su (?P<instance>.+)",
    "NodoMemoriaAlta": r"RAM alta su (?P<instance>.+)",
    "NodoMemoriaCritica": r"RAM critica su (?P<instance>.+)",
    "FilesystemQuasiPieno": r"Filesystem (?P<mountpoint>.+) quasi pieno su (?P<instance>.+)",
    "FilesystemPieno": r"Filesystem (?P<mountpoint>.+) pieno su (?P<instance>.+)",
    "FilesystemInodeEsauriti": r"Inode quasi esauriti su (?P<instance>.+) \((?P<mountpoint>.+)\)",
    "FilesystemSolaLettura": r"Filesystem in sola lettura su (?P<instance>.+)",
    "DiscoIOSaturo": r"Disco (?P<device>.+) saturo su (?P<instance>.+)",
    "NodoErroriRete": r"Errori di rete su (?P<instance>.+) \((?P<device>.+)\)",
    "NodoPacchettiScartati": r"Pacchetti scartati su (?P<instance>.+) \((?P<device>.+)\)",
    "NodoNonReady": r"Nodo (?P<node>.+) NotReady",
    "NodoSottoPressione": r"Nodo (?P<node>.+) in (?:MemoryPressure|DiskPressure|PIDPressure)",
    "NodeExporterGiu": r"node-exporter non raggiungibile su (?P<instance>.+)",
    "LonghornVolumeDegradato": r"Volume Longhorn (?P<volume>.+) degradato",
    "LonghornVolumeFaulted": r"Volume Longhorn (?P<volume>.+) FAULTED",
    "LonghornVolumeQuasiPieno": r"Volume Longhorn (?P<volume>.+) quasi pieno",
    "LonghornNodoNonPronto": r"Nodo Longhorn (?P<node>.+) non pronto",
    "LonghornNodoNonSchedulabile": r"Nodo Longhorn (?P<node>.+) non schedulabile",
    "LonghornDiscoQuasiPieno": r"Disco Longhorn (?P<disk>.+) su (?P<node>.+) quasi pieno",
    "XFSCorruzioneRilevata": r"Corruzione XFS rilevata su (?P<instance>.+)",
    "XFSFilesystemSpento": r"Filesystem XFS spento dal kernel su (?P<instance>.+)",
    "NodoFilesystemReadOnlyKernel": r"Filesystem rimontato in sola lettura su (?P<instance>.+)",
}
_RESOURCE_DESCRIPTIONS = {
    "FilesystemSolaLettura": r"^(?P<mountpoint>.+) è montato read-only",
    "LonghornVolumeDegradato": r"\(PVC (?P<pvc>[^\s,]+), ns (?P<namespace>[^\s)]+)\)",
    "LonghornVolumeFaulted": r"\(PVC (?P<pvc>[^\s)]+)\)",
}

_PERCENT_NAMES = {
    "Utilizzo CPU al ": "cpu_usage",
    "Memoria usata al ": "memory_usage",
    "Spazio usato al ": "space_usage",
    "Inode liberi: ": "free_inodes",
}
_NUMBER = r"(?:[+-]?(?:[0-9]+(?:\.[0-9]+)?|Inf)|NaN)"
_PERCENT = re.compile(
    rf"(?m)^(?P<prefix>{'|'.join(map(re.escape, _PERCENT_NAMES))})(?P<raw>{_NUMBER}%)(?=[\s.:]|$)"
)
_RATE = re.compile(
    rf"(?m)^(?P<raw>{_NUMBER} (?P<unit>errori/s|pacchetti/s))(?=[\s.]|$)"
)
_RATE_NAMES = {"errori/s": "network_errors", "pacchetti/s": "received_packets_dropped"}


def _resource(alertname: str, summary: str, description: str) -> AlertResource:
    fields = {}
    if pattern := _RESOURCE_SUMMARIES.get(alertname):
        if match := re.fullmatch(pattern, summary):
            fields.update(match.groupdict())
    if pattern := _RESOURCE_DESCRIPTIONS.get(alertname):
        if match := re.search(pattern, description):
            fields.update(match.groupdict())
    return AlertResource(
        **{
            key: value
            for key, value in fields.items()
            if value.strip() not in {"", "<no value>"}
        }
    )


def _metric(name: str, raw: str, unit: str) -> AlertMetric:
    value = Decimal(raw.removesuffix(unit).strip())
    return AlertMetric(
        name=name, raw=raw, unit=unit, value=value if value.is_finite() else None
    )


def _metrics(description: str) -> tuple[AlertMetric, ...]:
    matches = [
        (match.start(), _metric(_PERCENT_NAMES[match["prefix"]], match["raw"], "%"))
        for match in _PERCENT.finditer(description)
    ]
    matches.extend(
        (
            match.start(),
            _metric(_RATE_NAMES[match["unit"]], match["raw"], match["unit"]),
        )
        for match in _RATE.finditer(description)
    )
    return tuple(metric for _, metric in sorted(matches))


def parse_telegram_message(text: str) -> TelegramNotification | None:
    """Return a notification for the Italian Helm template, if recognized."""
    lines = text.splitlines()
    if len(lines) < 3 or not (header := _HEADER.fullmatch(lines[0].strip())):
        return None
    severity_label, separator, severity = lines[1].partition(":")
    if severity_label != "Gravità" or not separator:
        return None

    preamble, *blocks = _BULLET.split("\n".join(lines[2:]))
    if preamble.strip() or not blocks:
        return None

    alertname = header["alertname"]
    status = AlertStatus.FIRING if header["firing"] else AlertStatus.RESOLVED
    alerts = []
    for block in blocks:
        summary, _, description = block.partition("\n")
        summary, description = summary.strip(), description.strip()
        if not summary:
            continue
        # The template only renders group status, and a firing group can include resolved alerts.
        entry_status = None
        if status == AlertStatus.RESOLVED or (len(blocks) == 1 and description):
            entry_status = status
        alerts.append(
            AlertEntry(
                status=entry_status,
                summary=summary,
                description=description,
                resource=_resource(alertname, summary, description),
                metrics=_metrics(description),
            )
        )
    if not alerts:
        return None
    return TelegramNotification(
        text=text,
        alertname=alertname,
        severity=severity.strip() or None,
        status=status,
        alerts=tuple(alerts),
    )
