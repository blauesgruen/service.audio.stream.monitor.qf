# Radio Source Finder

Python-Tool mit GUI zur Analyse von Internet-Radio-URLs.

## Features
- Eingabe eines Sendernamens oder einer Sender-/Playlist-URL
- Automatische Sender-Suche per Name (Radio-Browser API)
- Sendernamen-Lookup mit generischen Token-Varianten (z. B. Teilphrasen), ohne sender-spezifische Sonderfaelle
- Auflösung zur Original-Stream-Quelle (Redirects + `m3u`/`pls`/`xspf`)
- Generische Now-Playing-Discovery (keine sender-spezifischen Hardcodes)
- Abruf der aktuellen Songinfo per ICY-Metadaten (`StreamTitle`)
- Discovery laeuft auch weiter, wenn ICY keinen `StreamTitle` liefert
- Bevorzugte Nutzung externer Feed-Quellen (`XML`/`JSON`/`HTML`) mit `artist/title`, falls auffindbar
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
- Kodi-Bridge-Contract (`ASM <-> ASM-QF`): pro angenommenem Request genau eine Response (inkl. `aborted` bei superseded)
- Der effektiv verwendete Sender wird von ASM-QF in `RadioMonitor.QF.Response.Meta` als `station_used` geliefert; ASM setzt daraus sein eigenes Label in ASM-Namespace
- Verified-Source-Fastpath prueft bekannte Quellen typgerecht zuerst (Stream via ICY, Feed via Feed-Probe)
- Result-Cache ist nachrangiger Fallback nur bei echtem `verified_source`-Miss; bei Fastpath-Probe-Miss (Feed/ICY ohne gueltiges Paar) wird Cache bewusst uebergangen
- Bei Fastpath-Probe-Miss kann ASM-QF direkt `no_hit` aus der bekannten Quelle liefern, statt sofort die Vollkette zu starten
- Vollkette (Lookup -> Resolve -> ICY -> Discovery) laeuft nur, wenn Fastpath und Result-Cache keinen Treffer liefern oder explizit erforderlich sind
- QF-Parity mit begrenztem Hold (`QF_HOLD_SECONDS_MAX`) fuer stabile, aber schnelle Song-Ende-Erkennung
- Konservatives Feed-Stale-Drop-Fenster (`QF_STALE_FEED_DROP_SECONDS`) gegen `hit/no_hit`-Flackern

## Start
```bash
python3 main.py
```

## Single Source Of Truth
- Kodi und GUI nutzen denselben Top-Level-Code aus diesem Addon-Verzeichnis (`app/`, `service.py`).
- Es gibt keine zweite Codekopie innerhalb des Repos.

## Ausfuehrliche Dokumentation
- [Dokumentationsindex](./dokumente/README.md)
- [Produktuebersicht](./dokumente/01_Produktuebersicht.md)
- [Bedienung der GUI](./dokumente/02_Bedienung_GUI.md)
- [Architektur und Datenfluss](./dokumente/03_Architektur_Datenfluss.md)
- [Now-Playing-Discovery und Origin-Regeln](./dokumente/04_NowPlaying_und_Origin_Regeln.md)
- [Datenbank, Konfiguration und Betrieb](./dokumente/05_Datenbank_Konfiguration_Betrieb.md)
- [Entwicklerdokumentation](./dokumente/06_Entwicklerdokumentation.md)
- [Senderlisten und Batchtests](./dokumente/senderlisten_und_batchtests/README.md)

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
