# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Pubblico primario da confermare. Dai flussi documentati si deduce che la dashboard sia destinata agli operatori del monitoraggio infrastrutturale che devono esaminare gli alert Telegram, ricostruire gli episodi e registrare gli esiti degli interventi.

## Product Purpose

Alert Intelligence trasforma le notifiche Telegram di monitoraggio in un registro consultabile di notifiche, eventi, incidenti e ticket. L’operatore può ritrovare un incidente per nome dell’alert o risorsa, ricostruirne la cronologia a partire dai messaggi originali e registrare la nota di chiusura.

## Positioning

Il registro conserva il messaggio originale anche quando il formato è sconosciuto o un evento non può essere associato a un incidente. I promemoria con lo stesso nome, risorsa e fonte alimentano la cronologia dell’incidente aperto senza perdere i singoli eventi.

## Operating Context

- Il collector legge la chat Telegram configurata e salva notifiche ed eventi in PostgreSQL. Il parser segue il template italiano del bot di monitoraggio.
- **Incidenti** offre ricerca per alert o risorsa, filtri per stato del ticket e accesso a cronologia, correlazioni e chiusura. **Notifiche** include i messaggi fuori formato e gli eventi senza incidente, con un collegamento all’incidente quando disponibile. Le date sono mostrate in UTC.
- La CLI gestisce anche la rielaborazione degli eventi storici e la registrazione delle correlazioni. L’API JSON espone le operazioni documentate in `/docs`.
- La dashboard è documentata per uso locale su `localhost` o `127.0.0.1`, con accesso alla macchina come controllo di accesso.

## Capabilities and Constraints

- Un evento `FIRING` apre un incidente con ticket o alimenta un incidente aperto compatibile. Se l’identità della risorsa è incompleta, l’evento apre un incidente distinto. Per XFS, il template identifica l’istanza NPD e il device rimane sconosciuto.
- `RESOLVED` può entrare nella cronologia di un incidente compatibile. La chiusura del ticket richiede una nota dell’operatore. Per un gruppo `FIRING` con più voci, lo stato della singola voce rimane indeterminato perché il template espone solo quello del gruppo.
- Le correlazioni tra famiglie di alert richiedono una mappatura verificata di nodo, device e volume, accompagnata dalla fonte dell’evidenza. La causalità resta da accertare.

## Brand Commitments

Il prodotto si chiama **Alert Intelligence**. La dashboard e il template di monitoraggio usano l’italiano. Gli stati del messaggio sorgente mantengono la terminologia `FIRING` e `RESOLVED`.

## Evidence on Hand

- [Istruzioni operative](README.md) e [template di monitoraggio](doc/raw/values-monitoring.yaml).
- [Test del parser](tests/test_telegram_parser.py) e [test della dashboard](tests/test_dashboard.py) con dati sintetici.

## Product Principles

1. Conservare il messaggio originale e renderlo consultabile anche quando il parsing o il collegamento all’incidente rimane incompleto.
2. Mantenere distinti lo stato riportato dal monitoraggio, la cronologia dell’incidente e la chiusura del ticket registrata dall’operatore.
3. Esplicitare le lacune nell’identità delle risorse e richiedere evidenza per collegare incidenti di famiglie diverse.
