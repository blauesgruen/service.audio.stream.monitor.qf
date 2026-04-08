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
- `epg`
- `feed_candidates`
- `origin_domains`
- `error`
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
   - XML/JSON Kandidaten abrufen
   - bestes `artist/title` ermitteln
7. Auswahl des finalen Songs
   - Feed-Song wird bevorzugt, wenn eindeutig und erlaubt
8. Polling in Intervallen (`SONG_REFRESH_INTERVAL_SECONDS`)
   - Songwechsel erkennen
   - Songende erkennen bei fehlendem klaren Song
9. Optional `SourceDatabase.upsert_verified_source()`

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
