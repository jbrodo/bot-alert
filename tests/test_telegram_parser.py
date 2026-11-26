from decimal import Decimal

import pytest

from app.parser.telegram import AlertStatus, parse_telegram_message


def test_preserves_group_text_and_original_metric_above_100():
    text = (
        "🔥 LonghornVolumeQuasiPieno\r\n"
        "Gravità: warning\r\n\r\n"
        "• Volume Longhorn pvc-abc quasi pieno\r\n"
        "Spazio usato al 136%.\r\n\r\n"
    )

    notification = parse_telegram_message(text)

    assert notification is not None
    assert notification.text == text
    assert notification.alertname == "LonghornVolumeQuasiPieno"
    assert notification.severity == "warning"
    assert notification.status == AlertStatus.FIRING
    (alert,) = notification.alerts
    assert alert.status == AlertStatus.FIRING
    assert alert.resource.volume == "pvc-abc"
    (metric,) = alert.metrics
    assert (metric.name, metric.raw, metric.unit, metric.value) == (
        "space_usage", "136%", "%", Decimal("136")
    )


def test_firing_group_keeps_all_entries_with_indeterminate_status():
    text = (
        "🔥 FilesystemPieno\nGravità: critical\n\n"
        "• Filesystem /var/lib/kubelet pieno su [fd00::1]:9100\n"
        "Spazio usato al 96%.\n\n"
        "• Filesystem /mnt/two words pieno su [fd00::1]:9100\n"
        "Spazio usato al 93%.\n"
        "Ulteriore contesto.\n"
    )

    notification = parse_telegram_message(text)

    assert notification.status == AlertStatus.FIRING
    first, second = notification.alerts
    assert first.status is None
    assert second.status is None
    assert first.resource.instance == "[fd00::1]:9100"
    assert first.resource.mountpoint == "/var/lib/kubelet"
    assert second.resource.mountpoint == "/mnt/two words"
    assert second.description == "Spazio usato al 93%.\nUlteriore contesto."


def test_resolved_group_marks_every_entry_resolved_and_extracts_pvc():
    text = (
        "✅ RISOLTO: LonghornVolumeDegradato\nGravità: warning\n\n"
        "• Volume Longhorn pvc-abc degradato\n"
        "Una o più repliche non sono sane (PVC database, ns production).\n\n"
        "• Volume Longhorn pvc-def degradato\n"
        "Una o più repliche non sono sane (PVC shared, ns staging).\n"
    )

    notification = parse_telegram_message(text)

    assert notification.status == AlertStatus.RESOLVED
    first, second = notification.alerts
    assert first.status == second.status == AlertStatus.RESOLVED
    assert first.resource.model_dump(exclude_none=True) == {
        "volume": "pvc-abc", "pvc": "database", "namespace": "production",
    }
    assert second.resource.namespace == "staging"


@pytest.mark.parametrize(
    ("alertname", "summary", "description", "resource"),
    [
        (
            "XFSFilesystemSpento",
            "Filesystem XFS spento dal kernel su 10.0.0.2:20257",
            "XFS è andato in shutdown: il volume non è più scrivibile.",
            {"instance": "10.0.0.2:20257"},
        ),
        (
            "LonghornDiscoQuasiPieno",
            "Disco Longhorn disk-1 su worker-1 quasi pieno",
            "Spazio usato al 88%.",
            {"disk": "disk-1", "node": "worker-1"},
        ),
        (
            "LonghornVolumeFaulted",
            "Volume Longhorn pvc-abc FAULTED",
            "Nessuna replica utilizzabile: dati non accessibili (PVC redis).",
            {"volume": "pvc-abc", "pvc": "redis"},
        ),
        (
            "NodoSottoPressione",
            "Nodo worker-2 in DiskPressure",
            "Kubelet segnala DiskPressure.",
            {"node": "worker-2"},
        ),
        (
            "FilesystemSolaLettura",
            "Filesystem in sola lettura su worker-1:9100",
            "/mnt/data è montato read-only (possibile errore disco).",
            {"instance": "worker-1:9100", "mountpoint": "/mnt/data"},
        ),
        (
            "LonghornNodoNonPronto",
            "Nodo Longhorn <no value> non pronto",
            "Longhorn non può usare i dischi di questo nodo.",
            {},
        ),
    ],
)
def test_resource_fields_from_annotations(alertname, summary, description, resource):
    notification = parse_telegram_message(f"🔥 {alertname}\nGravità: critical\n• {summary}\n{description}")

    assert notification.alerts[0].resource.model_dump(exclude_none=True) == resource


def test_decimal_rate_and_missing_group_severity():
    notification = parse_telegram_message(
        "🔥 NodoErroriRete\nGravità: \n• Errori di rete su 10.0.0.3:9100 (eth0)\n1.7 errori/s."
    )

    assert notification.severity is None
    (alert,) = notification.alerts
    assert alert.resource.device == "eth0"
    (metric,) = alert.metrics
    assert (metric.raw, metric.value, metric.unit) == ("1.7 errori/s", Decimal("1.7"), "errori/s")


def test_threshold_in_explanatory_text_is_not_a_measurement():
    notification = parse_telegram_message(
        "🔥 DiscoIOSaturo\nGravità: warning\n• Disco sda saturo su worker:9100\n"
        "Il disco è occupato oltre il 90% del tempo."
    )
    assert notification.alerts[0].metrics == ()


def test_preserves_nonfinite_metric_as_text():
    notification = parse_telegram_message(
        "🔥 LonghornVolumeQuasiPieno\nGravità: warning\n• Volume Longhorn pvc-abc quasi pieno\n"
        "Spazio usato al +Inf%."
    )
    (metric,) = notification.alerts[0].metrics
    assert metric.raw == "+Inf%"
    assert metric.value is None


def test_incomplete_entry_and_unknown_rule_remain_parseable():
    text = "🔥 RegolaNuova\nGravità: warning\n• Una risorsa"
    notification = parse_telegram_message(text)

    assert notification.text == text
    assert notification.alertname == "RegolaNuova"
    (alert,) = notification.alerts
    assert alert.status is None
    assert alert.resource.model_dump(exclude_none=True) == {}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Ciao!",
        "🔥 NodoCPUAlta\nGravità: warning",
        "🔥 NodoCPUAlta\nSeverity: warning\n• CPU alta",
        "🔥 NodoCPUAlta\nGravità: warning\n• ",
    ],
)
def test_unrecognized_messages_return_none(text):
    assert parse_telegram_message(text) is None
