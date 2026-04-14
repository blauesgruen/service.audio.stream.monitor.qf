# 06 Entwicklerdokumentation

## Zielgruppe

Dieses Dokument richtet sich an Entwickler, die das Tool erweitern, refactoren oder stabilisieren wollen.

## Projektstruktur (Entwicklersicht)

- `main.py`
  - Entry Point
- `app/gui.py`
  - Orchestrierung der Laufzeit und UI
- `app/models.py`
  - Dataklassen als Schnittstellenvertrag zwischen Modulen
- `app/config.py`
  - zentrale Runtime-Konstanten
- `app/station_lookup.py`
  - Name -> StationMatch
- `app/stream_resolver.py`
  - URL/Playlist -> ResolvedStream
- `app/metadata.py`
  - Stream-ICY -> SongInfo
- `app/now_playing_discovery.py`
  - Web-Discovery + XML/JSON/HTML Parsing -> SongInfo
- `app/epg_service.py`
  - EPG Probe -> EpgInfo
- `app/database.py`
  - Persistenz
- `app/live_logger.py`
  - thread-sicheres Logging
- `app/utils.py`
  - kleine, moduluebergreifende Utilities

## Laufzeitmodell

Der kritische Pfad liegt in `RadioToolApp._scan_worker()`:

1. optional Sender-Lookup
2. Stream-Aufloesung
3. Origin-Domain-Ermittlung
4. Start asynchroner EPG-Thread
5. Polling-Loop:
   - ICY lesen
   - Feed-Kandidaten entdecken (einmal)
   - Feed pollen
   - Song auswählen
   - Songwechsel/Songende erkennen

Hinweis:

- Wenn ICY in einem Poll-Zyklus keine verwertbaren Songdaten liefert, laeuft Feed-Discovery trotzdem weiter.

Kommunikation Worker -> UI erfolgt ausschliesslich ueber `self._results` Queue.

## Datenvertraege (`app/models.py`)

### `ResolvedStream`

- `input_url`: urspruengliche Nutzereingabe
- `resolved_url`: origin stream
- `delivery_url`: ggf. redirect ziel
- `content_type`: HTTP content-type
- `was_playlist`: playlist wurde aufgeloest
- `station_name`: optionaler Anzeigename

### `SongInfo`

- `stream_title`: Gesamtanzeige
- `raw_metadata`: Rohpayload (ICY/XML/JSON/HTML)
- `artist`: eindeutiger Artist, falls erkannt
- `title`: eindeutiger Titel, falls erkannt
- `source_kind`: `stream_icy`, `web_feed_xml`, `web_feed_json`, `web_feed_html`
- `source_url`: Quelle der Songdaten
- `source_approval`: `origin`, `official_player_chain` oder leer
- `source_headers`: Header-Snapshot (typisch ICY/HTTP)

### `StationMatch`

Radio-Browser Match inkl. `raw_record` fuer Rohdatenanzeige.

### `EpgInfo`

- `available`
- `source_url`
- `summary`
- `raw_xml`
- `error`

## Oeffentliche Klassen und Kernmethoden

### `StationLookupService` (`app/station_lookup.py`)

- `find_best_match(query: str) -> StationMatch`

Wichtig:

- nutzt mehrere Radio-Browser Mirrors (`RADIO_BROWSER_BASE_URLS`)
- erzeugt Lookup- und Slug-Varianten aus generischen Token-Fenstern (keine sender-festen Regeln)
- dedupliziert und scored Kandidaten
- Web-Fallback prueft mehrere Slug-Varianten (mit/ohne Bindestrich, mit/ohne `radio`)

### `StreamResolver` (`app/stream_resolver.py`)

- `resolve(input_url: str, original_input: str | None = None) -> ResolvedStream`

Wichtig:

- begrenzt durch `MAX_REDIRECTS`
- erkennt Playlists ueber Suffix/Content-Type
- extrahiert erste Stream-URL aus `m3u`, `pls`, `xspf`

### `SongMetadataFetcher` (`app/metadata.py`)

- `fetch(stream_url: str) -> SongInfo`

Wichtig:

- setzt Header `Icy-MetaData: 1`
- erwartet `icy-metaint`
- erkennt `artist/title` aus mehreren Stringmustern

### `NowPlayingDiscoveryService` (`app/now_playing_discovery.py`)

Oeffentliche Methoden:

- `discover_candidate_urls(resolved, station, stream_headers) -> list[str]`
- `fetch_now_playing(candidate_urls) -> SongInfo | None`
- `is_trusted_candidate(url) -> bool`
- `get_linked_domains() -> set[str]`

Wichtig:

- keine sender-spezifischen hardcodes
- Candidate Ranking + Filter + Key-Injection
- zusaetzliche generische Status-Seed-Endpunkte (`status-json.xsl`, `status.xsl`, `stats`)
- Frischelogik ueber `MAX_NOWPLAYING_AGE_MINUTES`
- zusaetzlich Dauerfenster pruefung ueber `starttime + duration + NOWPLAYING_DURATION_GRACE_SECONDS`
- `trusted` markiert Discovery-Quellen aus offizieller Player-Kette; nur mit `ALLOW_OFFICIAL_CHAIN_SOURCES=True` zusaetzlich erlaubt
- zusaetzliche generische Player-Config-Extraktion (`data-mandate` + `webradio.js` -> `config.json` -> `currentUrl`/`playlistUrl`)

### `EpgService` (`app/epg_service.py`)

- `fetch(stream_url, homepage_url="") -> EpgInfo`

Wichtig:

- best-effort probe ueber standardisierte SPI-Pfade
- harter Probe-Deckel (`max_probes`, Deadline)
- akzeptiert nur valides XML mit EPG-Hinweisen

### `SourceDatabase` (`app/database.py`)

- `upsert_verified_source(resolved, song, epg=None) -> None`

Wichtig:

- Schema-Migrationen per `_ensure_column`
- Upsert ueber unique `(input_url, resolved_url)`

### `RadioToolApp` (`app/gui.py`)

Laufzeitrelevante Methoden:

- `start_scan()`
- `_scan_worker(value)`
- `_consume_results()`
- `_render_source_details()`
- `save_verified()`

Wichtig:

- keine direkten UI-Updates aus Worker
- Songwechsel anhand `song_key = source_url|artist|title`
- Songende bei wiederholt fehlendem klaren Song

## Invarianten

- `config.py` ist Single Source of Truth fuer Runtime-Parameter.
- `models.py` bleibt schlank und transportorientiert (keine Businesslogik).
- Netzwerk-Operationen duerfen UI-Thread nicht blockieren.
- Origin-Only-Logik darf nicht durch sender-spezifische Sonderfaelle umgangen werden.
- Verzeichnis-/Aggregator-Domains (`radio.*`, `radio-assets.com`) duerfen nicht als Origin gelten.

## Erweiterungspunkte

### 1) Neue Discovery-Heuristik

Ort:

- `NowPlayingDiscoveryService._extract_urls_from_document`
- `_candidate_score`
- `_build_generated_candidates`

Regel:

- generische Muster statt station-fester Domains.

### 2) Neue Parsing-Felder

Ort:

- `TITLE_KEYS`, `ARTIST_KEYS`, `STATUS_KEYS`, `TIME_KEYS`, `DURATION_KEYS`

Regel:

- nur semantisch allgemeine Schluessel hinzufuegen.

### 3) Persistenz erweitern

Ort:

- `SourceDatabase._ensure_schema`
- `SourceDatabase.upsert_verified_source`

Regel:

- Migration immer rueckwaertskompatibel (`ALTER TABLE ... ADD COLUMN`).

### 4) UI-Events erweitern

Ort:

- Producer: `_scan_worker`
- Consumer: `_consume_results`

Regel:

- Eventnamen konsistent halten
- Payload-Struktur klar definieren

## Logging-Richtlinie

Technische Schluesselereignisse loggen:

- Quelle der Entscheidung (z. B. Feed-URL)
- Grund fuer Verwerfen (kein artist/title, stale, origin nicht erlaubt)
- Start/Stop und Fehlerpfade

Kein Logging sensibler Daten ausserdem noetigen HTTP-Metadaten.

## Kodi Bridge Contract (ASM <-> ASM-QF)

Kommunikation erfolgt ueber `Window(10000)`-Properties.

Request-Felder (`ASM` -> `ASM-QF`):

- `RadioMonitor.QF.Request.Id`
- `RadioMonitor.QF.Request.Station`
- `RadioMonitor.QF.Request.StationId`
- `RadioMonitor.QF.Request.Mode`
- `RadioMonitor.QF.Request.Ts`

Response-Felder (`ASM-QF` -> `ASM`):

- `RadioMonitor.QF.Response.Id`
- `RadioMonitor.QF.Response.Status`
- `RadioMonitor.QF.Response.Artist`
- `RadioMonitor.QF.Response.Title`
- `RadioMonitor.QF.Response.Source`
- `RadioMonitor.QF.Response.Reason`
- `RadioMonitor.QF.Response.Meta`
- `RadioMonitor.QF.Response.Ts`
- `RadioMonitor.QF.Response.ForReqId`
- `RadioMonitor.QF.Response.StationUsed`

### Verbindliche Regel

- Fuer jeden in `ASM-QF` angenommenen Request wird genau eine Response geschrieben.
- Das gilt auch fuer Abbruchpfade wie `request_superseded` (Status `aborted`).
- Keine stillen Returns ohne Response-Write nach `request_received`.

Parity-Detail:

- Die finale Hit/No-Hit-Entscheidung erfolgt in `_apply_qf_parity_policy`.
- Hold-Zeit ist gedeckelt: `effective_hold = min(QF_HOLD_SECONDS, QF_HOLD_SECONDS_MAX)`.
- Ein Feed-only-Hit wird erst nach `QF_STALE_FEED_DROP_SECONDS` als stale-only abgewertet.
- Bei bestaetigtem Songende wird der letzte Hit-Status atomar geloescht (keine teilweisen State-Resets).

Supersede-Policy:

- Default ist Preflight-Supersede (vor Start der Bearbeitung).
- Midflight-Supersede ist optional und standardmaessig deaktiviert.
- Schalter: `QF_SUPERSEDE_PREEMPT_ENABLED`, `QF_SUPERSEDE_MIDFLIGHT_ENABLED`.

### Erwartete Statuswerte

- `hit`
- `no_hit`
- `blocked`
- `aborted`
- `error`
- `timeout`

### Relevante QF-Parameter fuer Parity

- `QF_HOLD_SECONDS`
- `QF_HOLD_SECONDS_MAX`
- `QF_STALE_FEED_DROP_SECONDS`
- `QF_NO_HIT_CONFIRM`
- `QF_EMPTY_CONFIRM`

### Log-Checks fuer Contract-Verletzungen

Pruefen bei Laufzeitproblemen:

1. Auf jeden `event=request_received` folgt ein `event=request_result` mit gleicher `req_id`.
2. Auf jeden `event=request_result` folgt ein `event=response_written` mit gleicher `req_id`.
3. Bei `event=request_superseded_abort` muss ein `request_result status=aborted reason=request_superseded` erscheinen.
4. Bei `ASM-QF DIAG ... fresh_reason=missing_response_id` ueber laengere Zeit liegt meist ein Response-Contract-Bruch vor.

## Fehlerbilder und Debug-Strategie

### Fehlerbild: "Kein Song trotz sichtbarem Website-NowPlaying"

Pruefen:

1. `Entdeckte Song-Feed-Quellen` im Details-Fenster
2. Live-Log auf `Now-Playing Treffer aus Feed`
3. ob Quelle durch Origin-Only herausgefiltert wird
4. ob Feed stale ist (start/duration)

### Fehlerbild: "alte Songs bleiben stehen"

Pruefen:

1. Liefert Feed `starttime` und `duration`?
2. Passt Zeitzone/Format?
3. Werden neue Payloads abgeholt (Cache-Bust aktiv)?

## Build- und Sanity-Check

Empfohlener Schnellcheck nach Aenderungen:

```bash
python3 -m py_compile main.py app/*.py
```

Manueller Funktionstest:

1. Start mit Sendername
2. Start mit direkter URL
3. Ein Sender mit klarer JSON-Quelle
4. Ein Sender mit XML-Quelle
5. Speichern in DB und SQL-Check

## Konventionen fuer künftige Aenderungen

- Kein station-spezifischer Hardcode.
- Neue Grenzwerte nur in `config.py`.
- Modullogik in Modul lassen, GUI nur orchestrieren.
- Fehlermeldungen fuer User knapp, technische Details ins Live-Log.

## Dateiindex fuer schnelle Navigation

- `main.py`
- `app/gui.py`
- `app/now_playing_discovery.py`
- `app/metadata.py`
- `app/stream_resolver.py`
- `app/station_lookup.py`
- `app/epg_service.py`
- `app/database.py`
- `app/models.py`
- `app/config.py`
- `app/utils.py`
- `app/live_logger.py`
