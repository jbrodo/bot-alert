# Roadmap di Alert Intelligence

Alert Intelligence raccoglie notifiche Telegram, le organizza in incidenti e consente agli operatori di registrare gli esiti. Il prossimo traguardo è completare il triage dalla dashboard e usare l’intelligenza artificiale (IA) per generare un riepilogo verificabile di un incidente.

## 1. Correlazioni verificate dalla dashboard

Dal dettaglio di un incidente, l’operatore indica il secondo incidente, compila la mappatura delle risorse e annota la fonte dell’evidenza. Il form riusa le stesse regole di `app.tickets correlate`.

**Completata quando** una mappatura valida compare in entrambi gli incidenti, una mappatura incompatibile produce un errore accanto al form conservando i dati inseriti e un secondo invio lascia una sola correlazione.

## 2. Ricerca nel registro Telegram

Aggiungere ricerca per nome dell’alert e identificativo della risorsa, con filtro per intervallo temporale. Combinare la ricerca con i filtri esistenti, compresi **Fuori formato** e **Senza incidente**, e mantenere i criteri quando l’operatore ritorna dal dettaglio di un incidente.

**Completata quando** l’operatore può restringere una ricerca, consultare i messaggi originali anche quando il parser non li riconosce e tornare alla stessa pagina dei risultati con i filtri ancora attivi.

## 3. Riepilogo dell’incidente con IA

Aggiungere l’azione **Genera riepilogo con IA** al dettaglio dell’incidente. Il modello riceve un insieme limitato di eventi dell’incidente selezionato, identificati da riferimenti interni e orari UTC. Produce una bozza in italiano con una breve cronologia, l’ultimo stato riportato dal monitoraggio, le evidenze principali e gli elementi che restano da verificare.

- **Dati in ingresso.** Usare i campi già estratti, tra cui stato, orario, risorsa, sintesi e metriche pertinenti. I riferimenti sono associati nell’applicazione alle notifiche originali. Se l’incidente contiene più eventi di quelli inclusi, indicare quante voci sono state considerate.
- **Risultato ed errori.** Ogni fatto riportato deve puntare a uno o più eventi effettivamente inclusi. L’applicazione verifica la struttura della risposta e l’esistenza dei riferimenti prima di mostrarla. In caso di risposta non valida o servizio indisponibile, mostra un messaggio in italiano e consente di riprovare.
- **Responsabilità.** La generazione si avvia su richiesta dell’operatore. Il riepilogo è una bozza di sola lettura. L’apertura, la correlazione e la chiusura continuano a seguire le regole del prodotto e, quando prevista, la conferma dell’operatore.
- **Prima prova.** Usare dati sintetici per verificare il formato e confrontare i riepiloghi con gli eventi sorgente. La scelta tra un modello locale e un servizio esterno resta aperta fino alla valutazione dei dati che l’ambiente può inviare.

**Completata quando** un test in dashboard produce una bozza con riferimenti apribili. Le verifiche coprono un incidente con stato ambiguo, un incidente con più eventi di quelli inclusi, un riferimento inventato e un errore del modello. In tutti i casi il registro e il ticket conservano il proprio stato.

## 4. Verifica operativa del collector

Rendere consultabile l’ultimo messaggio acquisito e documentare come diagnosticare un errore di raccolta. Verificare il percorso dalla chat alla dashboard in un ambiente di test.

**Completata quando** una prova end-to-end dimostra che un `FIRING` apre un incidente, un `RESOLVED` compatibile entra nella cronologia e un operatore può chiudere il ticket con una nota.

## 5. Storico delle ricorrenze

Mostrare gli episodi precedenti per alert e risorsa, con il numero di notifiche e le note di chiusura.

**Completata quando** una ricerca per risorsa mostra gli episodi in ordine cronologico e distingue i conteggi di incidenti e notifiche.
