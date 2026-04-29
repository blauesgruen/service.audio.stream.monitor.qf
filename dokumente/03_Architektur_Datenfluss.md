# 03 Architektur und Datenfluss

## Moduluebersicht

- `main.py`
  - Einstiegspunkt, startet `run_app()`.
- `app/gui.py`
  - Tkinter-Oberflaeche, Worker-Thread, Event-Queue, Live-Status.
- `app/station_lookup.py`
  - Sendername -> bestes Radio-Browser-Match.
- `app/stream_resolver.py`
  - URL-Aufloesung inkl. Redirects und Playlist-Parsing.
- `app/metadata.py`
  - ICY-Metadaten lesen (`StreamTitle`, Header).
- `app/now_playing_discovery.py`
  - Generische Suche nach XML/JSON-Songquellen und Parsing.
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
  - Hilfsfunktionen (`base_domain`, URL-Checks, `origin`-Checks).

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
   - Name -> `StationLookupService` -> Stream-Seed
3. `StreamResolver.resolve()`
   - Redirect verfolgen
   - Playlists erkennen und erste Stream-URL extrahieren
4. `SongMetadataFetcher.fetch()`
   - ICY lesen
   - `StreamTitle`, `artist/title` Versuch
5. `NowPlayingDiscoveryService.discover_candidate_urls()`
   - Homepage/Seeds/Skripte scannen
   - zusaetzlich typische Icecast/Shoutcast-Statuspfade pruefen (`status-json.xsl`, `status.xsl`, `stats`)
   - Kandidaten ranken und begrenzen
6. `NowPlayingDiscoveryService.fetch_now_playing()`
   - XML/JSON/HTML Kandidaten abrufen (seriell oder parallel in Batches)
   - bestes `artist/title` ermitteln
7. Auswahl des finalen Songs
   - Feed-Song wird bevorzugt, wenn eindeutig und erlaubt
8. Polling in Intervallen (`SONG_REFRESH_INTERVAL_SECONDS`)
   - Songwechsel erkennen
   - Songende erkennen bei fehlendem klaren Song
9. Optional `SourceDatabase.upsert_verified_source()`

## Kodi-Bridge-Datenfluss (`service.py`)

1. `ASM` schreibt Request-Properties (`RadioMonitor.QF.Request.*`) mit `req_id`.
2. `ASM-QF` liest Request, verarbeitet Lookup/Resolve/ICY/Discovery.
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
- Vollkette: Lookup -> Resolve -> ICY -> Discovery -> Policy/Parity-Entscheidung, nur wenn Fastpath/Cache keinen verwertbaren Zustand liefern.
- Discovery priorisiert Kandidaten zentral: offizielle HTML-Now-Playing-Kandidaten zuerst, danach starke strukturierte Feed-URLs, dann Rest.

### Parity-Entscheidung (Kodi-Bridge)

- Die finale Entscheidung fuer `hit`/`no_hit` laeuft zentral in `QFBridgeService._apply_qf_parity_policy`.
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
- Zentral: Konstanten in `config.py`, Modelle in `models.py`.
- Transparenz: Live-Log plus Rohdaten-Details.
- Robustheit: mehrere Discovery-Seeds und heuristische Kandidatenbewertung statt Hardcode.
