# Radio Source Finder

Python-Tool mit GUI zur Analyse von Internet-Radio-URLs.

## Features
- Eingabe eines Sendernamens oder einer Sender-/Playlist-URL
- Automatische Sender-Suche per Name (Radio-Browser API)
- Auflösung zur Original-Stream-Quelle (Redirects + `m3u`/`pls`/`xspf`)
- Generische Now-Playing-Discovery (keine sender-spezifischen Hardcodes)
- Abruf der aktuellen Songinfo per ICY-Metadaten (`StreamTitle`)
- Discovery laeuft auch weiter, wenn ICY keinen `StreamTitle` liefert
- Bevorzugte Nutzung externer Feed-Quellen (`XML`/`JSON`) mit `artist/title`, falls auffindbar
- Generische Icecast/Shoutcast-Statusquellen werden mitberuecksichtigt (`status-json.xsl`, `status.xsl`, `stats`)
- Anzeige der tatsächlich genutzten Song-Quelle (`source_url`) im Quell-Details-Fenster
- `Origin-only` aktiv mit optionaler `offizieller Player-Kette`:
  - Origin-Domain immer erlaubt
  - optional zusaetzlich: offiziell verlinkte Player-Feeds (z. B. `.../webradio/.../current/...json`)
  - Verzeichnis-/Aggregator-Domains (`radio.*`, `radio-assets.com`) bleiben ausgeschlossen
- Trennung von `Origin-Stream` und `Delivery-URL` (Redirect-Ziel)
- Web-Fallback bei Sendernamen prueft mehrere Slug-Varianten (mit/ohne Bindestrich, mit/ohne `radio`)
- Feed-Daten werden auf Frische geprüft; veraltete `now`-Einträge werden verworfen
- Songanzeige nur bei eindeutigem `artist` + `title`
- Best-Effort EPG-Probe (falls Sender EPG/SI.xml online bereitstellt)
- Live-Log in separatem Fenster
- Quell-Details-Fenster mit Rohdaten (Lookup-JSON, Header, Raw-Metadaten, EPG-XML)
- Speicherung verifizierter Quellen in SQLite (`radio_sources.db`)

## Start
```bash
python3 main.py
```

## Ausfuehrliche Dokumentation
- [Dokumentationsindex](./dokumente/README.md)
- [Produktuebersicht](./dokumente/01_Produktuebersicht.md)
- [Bedienung der GUI](./dokumente/02_Bedienung_GUI.md)
- [Architektur und Datenfluss](./dokumente/03_Architektur_Datenfluss.md)
- [Now-Playing-Discovery und Origin-Regeln](./dokumente/04_NowPlaying_und_Origin_Regeln.md)
- [Datenbank, Konfiguration und Betrieb](./dokumente/05_Datenbank_Konfiguration_Betrieb.md)
- [Entwicklerdokumentation](./dokumente/06_Entwicklerdokumentation.md)

## Verifizierung + DB
Eine Quelle gilt als verifiziert, sobald eine Stream-URL aufgelöst und Song-Metadaten erkannt wurden.
Danach kann sie über **"Verifiziert speichern"** in die DB geschrieben werden.

## Struktur
- `main.py`: Einstiegspunkt
- `app/config.py`: zentrale Konstanten
- `app/models.py`: zentrale Datenmodelle
- `app/station_lookup.py`: Sendername -> Stream-URL
- `app/stream_resolver.py`: URL-/Playlist-Auflösung
- `app/metadata.py`: Song-Metadaten-Leser
- `app/now_playing_discovery.py`: generische Feed-Suche + artist/title-Parsing
- `app/epg_service.py`: EPG-Probe und XML-Zusammenfassung
- `app/database.py`: SQLite-Logik
- `app/live_logger.py`: Thread-sicheres Logging
- `app/utils.py`: zentrale Helper
- `app/gui.py`: grafische Oberfläche
