# 03 Architektur und Datenfluss

## Moduluebersicht

- `main.py`
  - Einstiegspunkt, startet `run_app()`.
- `app/gui.py`
  - Tkinter-Oberflaeche, optionaler `Station-ID`-/Slug-Hint, Worker-Thread, Event-Queue, Live-Status.
- `app/station_lookup.py`
  - Sendername oder Station-ID -> bestes Match inkl. Web-Fallback.
- `app/stream_resolver.py`
  - URL-Aufloesung inkl. Redirects und Playlist-Parsing.
- `app/metadata.py`
  - ICY-Metadaten lesen (`StreamTitle`, Header) und Metadaten-Texte zentral normalisieren.
- `app/now_playing_discovery.py`
  - Generische Suche nach XML/JSON/JSONP/HTML-Songquellen und Parsing inkl. offizieller GraphQL-Track-Feeds.
- `app/station_identity.py`
  - Gemeinsame Stations-Normalisierung, ID-First-Lookup, Variantenbildung und `station_key`-Helfer.
- `app/source_policy.py`
  - Gemeinsame Origin-Domain-Ermittlung und Source-Policy-Klassifikation.
- `app/song_probe.py`
  - Gemeinsamer Probe-/Auswahlkern fuer ICY, Feed-Discovery und finalen Song.
- `app/song_parity.py`
  - Gemeinsame Song-Zustandsmaschine fuer Hold, Songende, Stale-Guard und Reappearance-Sperre.
- `app/epg_service.py`
  - Best-effort EPG/SPI Probe.
- `app/database.py`
  - SQLite-Schema und Upsert verifizierter Quellen.
- `app/live_logger.py`
  - Thread-sicheres Queue-Logging.
- `app/models.py`
  - Gemeinsame Dataklassen (`ResolvedStream`, `SongInfo`, `StationMatch`, `EpgInfo`).
- `app/config.py`
  - Zentrale Konstanten.
- `app/utils.py`
  - Hilfsfunktionen (`base_domain`, URL-Checks, `origin`-Checks, Text-Normalisierung).

## Threading-Modell

- UI-Thread:
  - rendert Oberflaeche
  - konsumiert Events aus `self._results`
  - leert Live-Log-Queue
- Worker-Thread (`_scan_worker`):
  - durchlaeuft Lookup/Resolve/Metadata/Discovery/Polling
- Zusaetzlicher EPG-Thread:
  - laeuft parallel und blockiert Song-Erkennung nicht

## Event-Modell (`_results` Queue)

Events, die vom Worker an die UI geschickt werden:

- `station`
- `resolved`
- `song`
- `song_cleared`
- `song_state`
- `epg`
- `epg_disabled`
- `feed_candidates`
- `origin_domains`
- `error`
- `batch_progress`
- `batch_done`
- `done`

Die UI aktualisiert damit gezielt einzelne Felder.

## End-to-End-Datenfluss

1. Eingabe vom Nutzer
2. Entscheidung:
   - URL -> direkt `StreamResolver`
   - Name mit optionaler `Station-ID` -> `find_station_with_optional_id(...)` -> `StationLookupService` -> Stream-Seed
   - Name ohne `Station-ID` -> `find_station_by_name_with_fallback(...)` -> `StationLookupService` -> Stream-Seed
3. `StreamResolver.resolve()`
   - Redirect verfolgen
   - Playlists erkennen und erste Stream-URL extrahieren
4. `collect_origin_domains(...)`
   - Origin-Basisdomains aus Station + aufgeloestem Stream ableiten
5. `SongProbeSession.probe_once()`
   - ICY lesen
   - Feed-Kandidaten entdecken und priorisieren
   - Redirect-/Canonical-nahe Discovery-Dokumente und offizielle Player-Ketten nach weiteren Feed-Kandidaten scannen
   - XML/JSON/HTML Kandidaten abrufen (seriell oder parallel in Batches)
   - finalen Song zentral anhand Pair-Validierung und Source-Policy bestimmen
6. `SongParityPolicy.apply(...)`
   - bestaetigt Hits
   - erkennt Songende
   - blockt identische Wiedererscheinung nach Songende
7. Polling in Intervallen (`SONG_REFRESH_INTERVAL_SECONDS`)
   - Songwechsel erkennen
   - Songende erkennen bei fehlendem klaren Song
8. Optional `SourceDatabase.upsert_verified_source()`

## Kodi-Bridge-Datenfluss (`service.py`)

1. `ASM` schreibt Request-Properties (`RadioMonitor.QF.Request.*`) mit `req_id`.
   - optional inkl. `RadioMonitor.QF.Request.StationId` als stabile Radio-Browser-UUID oder Slug-Hinweis
2. `ASM-QF` liest Request, verarbeitet Fastpath/Cache und faellt bei Bedarf auf die gemeinsame
   Vollkette aus `app/` zurueck.
3. Ergebnis wird als Response (`RadioMonitor.QF.Response.*`) geschrieben.
   - inkl. `RadioMonitor.QF.Response.Meta.station_used` (effektiv verwendeter Sender in QF)
   - ASM uebernimmt diesen Wert und schreibt das Label in seinem eigenen Namespace (nicht als eigenes QF-Response-Feld)
4. Auch bei ueberholtem Request (superseded) schreibt `ASM-QF` eine Response mit `status=aborted`.

### Aktuelle Laufketten in `ASM-QF`

- `verified_source_fastpath`: verifizierte Quelle wird zuerst direkt geprueft.
  - Stream-Quelle -> ICY-Probe
  - Feed-Quelle -> Feed-Probe (`fetch_now_playing`)
  - Stream-Fastpath ist zusaetzlich durch Mindest-Confidence und optionalen `stream_confirmed`-Nachweis abgesichert.
- `result_cache_hit`: schneller In-Memory-Fallback nur bei echtem Fastpath-Miss ohne Probe-Treffer (`fastpath_state=miss`).
- `result_cache_bypassed_verified_probe_state`: ein alter Cache-Hit wird verworfen, wenn die verifizierte Quelle zwar geprobt wurde, aber aktuell kein gueltiges Paar liefert.
- `result_cache_bypassed_pair_changed`: ein alter Cache-Hit wird bewusst verworfen, wenn ein frischer Fastpath ein anderes `artist/title`-Paar liefert.
- `resolution_cache_hit`: Sender-/Resolve-Daten aus In-Memory-Resolution-Cache innerhalb der Vollkette.
- Bei Probe-Miss der verifizierten Quelle kann direkt `no_hit` aus dem Fastpath-Zweig geliefert werden (ohne sofortige Vollkette).
- Vollkette: gemeinsamer Lookup-Fallback -> Resolve -> gemeinsame Origin-Domain-Ermittlung ->
  gemeinsamer Probe-Kern (`SongProbeSession`) -> gemeinsame Song-Parity (`SongParityPolicy`) ->
  Policy/Parity-Entscheidung, nur wenn Fastpath/Cache keinen verwertbaren Zustand liefern.
- Bei gesetzter `StationId` versucht der gemeinsame Lookup-Pfad zuerst `find_by_id(...)`; bei Miss folgt der normale Namenspfad.
- Discovery priorisiert Kandidaten zentral: offizielle HTML-Now-Playing-Kandidaten zuerst, danach starke strukturierte Feed-URLs, dann Rest.

### Parity-Entscheidung (Kodi-Bridge)

- Die finale Entscheidung fuer `hit`/`no_hit` laeuft fachlich zentral in `app/song_parity.py`;
  `QFBridgeService._apply_qf_parity_policy` ergaenzt nur noch Request-Gap-Telemetrie und Trace-Logging.
- `QF_HOLD_SECONDS` wird zur Laufzeit durch `QF_HOLD_SECONDS_MAX` hart gedeckelt (aktuell 3.0s).
- Feed-only-Hits mit schwachem Stream-Signal werden nicht mehr nach wenigen Sekunden verworfen,
  sondern erst nach `QF_STALE_FEED_DROP_SECONDS` (konservatives Drop-Fenster).
- Bei bestaetigtem Songende (`no_hit`/`empty` bestaetigt) wird der letzte Hit-Status atomar geloescht.
- Danach merkt sich ASM-QF das zuletzt beendete Paar (`artist/title/source/source_url`) und blockt
  ein identisches Wiederauftauchen fuer `QF_REAPPEAR_BLOCK_SECONDS` (Default 600s).

### station_key-Fallback

- Name-Varianten koennen ueber einen konservativen Name-Fallback als kompatibel behandelt werden
  (DB-Lookup, Cache-Lookup, Supersede-Erkennung), konfigurierbar ueber `app/config.py`.

## Fehlerbehandlung (hoch-level)

- Netzwerk- und Parse-Fehler werden in Log und Status gespiegelt.
- Wenn ICY-Songdaten fehlen, laeuft Discovery/Feed-Abfrage trotzdem weiter.
- SSL-Zertifikatsprobleme werden teilweise mit unverified SSL fallback behandelt.
- Harte Abstuerze im Worker werden als `Unerwarteter Fehler` in die UI gemeldet.

## Designentscheidungen

- Modular: klare Trennung von Lookup, Resolve, Metadata, Discovery, Storage.
- Zentral: Konstanten in `config.py`, Modelle in `models.py`, Shared-Kernlogik in `station_identity.py`,
  `source_policy.py`, `song_probe.py` und `song_parity.py`.
- Wiederverwendung: Zeitfenster-, Frische- und Text-Normalisierung sollen moeglichst zentral bleiben und nicht in einzelnen Feed-Pfaden dupliziert werden.
- Transparenz: Live-Log plus Rohdaten-Details.
- Robustheit: mehrere Discovery-Seeds und heuristische Kandidatenbewertung statt Hardcode.
