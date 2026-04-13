# AGENTS Guide: service.audio.stream.monitor.qf

## Scope und Prioritaeten
- Dieses Repo ist ein **Kodi-Service + Desktop-GUI** fuer dieselbe Kernlogik; `app/` ist die Single Source of Truth.
- Bevor du Architekturannahmen triffst, lies `README.md`, `dokumente/03_Architektur_Datenfluss.md`, `dokumente/04_NowPlaying_und_Origin_Regeln.md`, `dokumente/06_Entwicklerdokumentation.md`.
- Gefundene AI-Regeldateien via Glob: `README.md`-Dateien plus dieses `AGENTS.md`; keine weiteren Regeldateien (`.cursorrules`, `CLAUDE.md`, etc.) vorhanden.

## Big Picture Architektur
- Desktop-Einstieg: `main.py` -> `app.gui.run_app()`.
- Kodi-Einstieg: `addon.xml` startet `service.py` (`QFBridgeService`) beim Login.
- Kernmodule: `app/station_lookup.py`, `app/stream_resolver.py`, `app/metadata.py`, `app/now_playing_discovery.py`, `app/epg_service.py`, `app/database.py`.
- Datenvertraege liegen in `app/models.py` (`ResolvedStream`, `SongInfo`, `StationMatch`, `EpgInfo`) und werden quer durch alle Module genutzt.
- Konfiguration zentral in `app/config.py`; neue Grenzwerte dort, nicht inline in Worker/Services.

## Datenfluesse, die man kennen muss
- GUI-Flow (`app/gui.py`, `RadioToolApp._scan_worker`): Input -> optional Name-Lookup -> Resolve -> Origin-Domains -> Polling (ICY + Feed-Discovery) -> Songwahl -> optionale DB-Speicherung.
- UI-Thread wird nur ueber Queue-Events aktualisiert (`station`, `resolved`, `song`, `song_cleared`, `song_state`, `epg`, `epg_disabled`, `feed_candidates`, `origin_domains`, `batch_progress`, `batch_done`, `error`, `done`).
- Kodi-Flow (`service.py`): Request/Response ueber `Window(10000)`-Properties, z. B. `RadioMonitor.QF.Request.*` und `RadioMonitor.QF.Response.*`.
- Kodi-Bridge blockt Requests, wenn Setting `provider_finder_enabled` aus ist (`resources/settings.xml`, `service.py:_handle_request`).

## Projekt-spezifische Regeln und Muster
- Song wird nur akzeptiert bei eindeutigem Paar (`artist` + `title`) via `prefilter_pair`/`is_valid_song_candidate` (`app/song_validation.py`, Aufrufe in GUI/Service).
- Origin-Policy ist hart: `ORIGIN_ONLY_MODE` + optional `ALLOW_OFFICIAL_CHAIN_SOURCES` (`app/config.py`); Verzeichnisdomains wie `radio.*` und `radio-assets.com` sind ausgeschlossen.
- Discovery ist weitgehend generisch (Seed-Scan, Ranking, Key-Injection), aber es gibt auch spezifische Kontexthelfer (z. B. BR-Kontext in `app/now_playing_discovery.py:_is_br_context`).
- In GUI und Service ist die Reihenfolge wichtig: erst ICY pruefen, Feed-Discovery trotzdem weiterlaufen lassen, dann finalen Song anhand Source-Policy freigeben.
- `resolved_url` (Origin) und `delivery_url` (Redirect-Ziel) werden getrennt behandelt; nicht zusammenwerfen.

## Persistenz und Integrationen
- GUI speichert in `radio_sources.db` via Tabelle `verified_sources` (`app/database.py`, unique `(input_url, resolved_url)`).
- Kodi-Bridge schreibt separat in `special://userdata/addon_data/<addon_id>/song_data.db`, Tabelle `verified_station_sources` (`service.py`).
- Externe Integrationen: Radio-Browser Mirrors (`RADIO_BROWSER_BASE_URLS`), HTTP/ICY Streams, XML/JSON/HTML-NowPlaying-Feeds, optionale EPG-SPI-Pfade.

## Entwickler-Workflows (real im Repo verankert)
- Start GUI laut Doku: `python3 main.py` (README); unter Windows typischerweise `python main.py`.
- Schneller Syntaxcheck laut Doku: `python3 -m py_compile main.py app/*.py`.
- Batchtests laufen ueber GUI (`Batchtest Datei...`) und schreiben TSV nach `dokumente/senderlisten_und_batchtests/batchtest_result_YYYYMMDD_HHMMSS.tsv`.
- Debug primar ueber Live-Log + Quell-Details-Fenster (`app/gui.py:_render_source_details`) sowie strukturierte Kodi-Logs (`QFLogger` in `service.py`).

## Aenderungsleitlinien fuer Agents
- Keine direkten UI-Updates aus Worker-Threads; nur Queue-Events verwenden.
- Keine sender-spezifischen Sonderfaelle als erste Wahl; erst generische Heuristiken erweitern.
- Neue DB-Felder rueckwaertskompatibel migrieren (`ALTER TABLE ... ADD COLUMN` Muster aus `app/database.py`).
- Bei neuen Request/Response-Feldern in Kodi immer beide Seiten konsistent halten (REQ/RES-Konstanten + Schreib/Lese-Pfade in `service.py`).
