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
