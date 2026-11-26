# Alert Intelligence

Il parser legge il testo restituito da Telegram in `Message.text` per il
template italiano in [values-monitoring.yaml](doc/raw/values-monitoring.yaml).

```python
from app.parser.telegram import parse_telegram_message

notification = parse_telegram_message(telegram_text)
```

Un risultato contiene il testo originale, i dati comuni del gruppo e una voce
per ogni alert. In un gruppo con più voci e stato `FIRING`, lo stato delle
singole voci è `None` perché il template non lo espone. I campi risorsa
provengono dalle annotazioni ricevute. I valori metrici mantengono il testo
ricevuto, accanto a un `Decimal` per i valori finiti. I messaggi fuori formato
restituiscono `None`.

Esegui i test con Python 3.12 o successivo e `uv run pytest`. Le fixture
riproducono il template Helm con dati sintetici.

## Collector Telegram

Il collector usa il long polling e accetta i messaggi di testo del bot di
monitoring nella chat configurata. Servono un bot collector dedicato,
senza webhook attivo, e queste variabili d'ambiente.

| Variabile | Valore |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token del bot collector |
| `TELEGRAM_CHAT_ID` | ID numerico del gruppo di monitoring |
| `TELEGRAM_MONITOR_BOT_ID` | ID numerico del bot mittente |
| `DATABASE_URL` | DSN PostgreSQL con permessi di creazione delle tabelle |

Aggiungi il collector al gruppo, abilita Bot-to-Bot in BotFather e disabilita
Group Privacy Mode oppure rendi il collector amministratore del gruppo.
[Documentazione Telegram](https://core.telegram.org/api/bots/bot-to-bot)

```sh
uv run python -m app.telegram.collector
```

Esegui una sola istanza per token. Il log mostra gli ID dei messaggi salvati
e lo stato del gruppo quando riconosciuto. Per verificare la consegna reale
cerca un `FIRING` e un `RESOLVED` inviati dal bot di monitoring.

`telegram_notifications` conserva il testo originale anche quando il formato
è sconosciuto. `alert_events` contiene una riga per voce riconosciuta. Le
riconsegne dello stesso messaggio sono idempotenti. Ogni nuovo messaggio viene
registrato, compresi i promemoria con testo identico. In caso di errore di
scrittura, il processo termina prima di confermare l'update a Telegram.

Per eseguire anche il test di integrazione, imposta `TEST_DATABASE_URL` su un
database di test con permessi di creazione di uno schema ed esegui
`uv run pytest`. Lo schema di test viene annullato al termine.

## Incidenti e ticket

Un evento `FIRING` apre un incidente con ticket. I promemoria con lo stesso
nome, risorsa e fonte alimentano l'incidente aperto e restano nel registro
degli eventi. Quando l'identità della risorsa è incompleta, il singolo evento
mantiene un incidente distinto. Per XFS l'identità disponibile è quella
dell'istanza NPD. Il template attuale non identifica il device.

Un evento `RESOLVED` può essere aggiunto alla cronologia di un incidente
compatibile. La chiusura richiede una nota dell'operatore. Gli eventi con
stato indeterminato rimangono consultabili in `alert_events`.

Con le stesse variabili d'ambiente del collector, puoi consultare i ticket,
rielaborare gli eventi salvati prima di questa versione e chiudere un ticket.

```sh
uv run python -m app.tickets list
uv run python -m app.tickets backfill
uv run python -m app.tickets close 42 --resolution "Ripristino verificato sul device"
```

Per collegare incidenti di famiglie diverse, usa gli ID incidente mostrati da
`list` e un file JSON con la mappatura verificata per quegli episodi.
Servono `node`, `device`, `volume` e `source`. In `source` registra
l'evidenza che lega le risorse a quegli episodi, per esempio il riferimento al
log kernel e alla verifica del volume sul nodo. Per XFS, `xfs_instance`
deve coincidere con l'istanza NPD riportata. Aggiungi `kubernetes_instance`,
`mountpoint` o `longhorn_disk` quando compaiono nei rispettivi incidenti.

```json
{
  "node": "worker-1",
  "device": "dm-0",
  "volume": "pvc-abc",
  "source": "Verifica inventario e log kernel registrata nel ticket operativo 123",
  "xfs_instance": "10.0.0.2:20257"
}
```

```sh
uv run python -m app.tickets correlate 15 16 --mapping mapping.json
```

Il collegamento viene mostrato su entrambi gli incidenti. Rappresenta una
relazione verificata tra risorse. La causalità dello shutdown resta da accertare.
