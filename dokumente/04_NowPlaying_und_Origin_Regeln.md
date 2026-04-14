# 04 Now-Playing-Discovery und Origin-Regeln

## Ziel

Die Discovery soll eine Quelle finden, die den aktuell gespielten Song mit expliziten Feldern liefert:

- `artist`
- `title`

Reine Stream-Texte ohne klare Trennung sind nur Fallback.

## Prinzip: keine sender-spezifischen Hardcodes

Das System arbeitet generisch ueber:

- URL-Muster
- typische API-/Feed-Keywords
- HTML/JS-Link-Extraktion
- JSON/XML-Heuristiken
- Plausibilitaets- und Frischepruefung

## Seed-Generierung

Kandidaten werden initial aus mehreren Quellen erzeugt:

- Stations-Homepage
- `icy-url` aus Stream-Headern
- Input-/Resolved-URL
- Host-Root (`https://host/`)
- typische Icecast/Shoutcast-Statuspfade (`/status-json.xsl`, `/status.xsl`, `/stats`)
- Basisdomain-Fallbacks (`https://www.<base>/`, `.../streams.json`)

Dadurch wird Discovery auch dann moeglich, wenn der direkte Stream selbst keine Songdaten liefert.

## Dokument-Scan

Aus Seeds werden Inhalte geholt und nach URLs durchsucht:

- absolute URLs
- relative `href/src/data-*`
- XML/JSON-Pfade
- Script-Assets (`.js`) auf gleicher Basisdomain

Zusatzlogik:

- `avcustom`-Dokumente werden extra verfolgt
- Audio-/Video-Content wird als Discovery-Text verworfen

## Kandidaten-Ranking

Die Bewertung kombiniert u. a.:

- Dateityp (`.xml`, `.json`)
- Keywords (`nowplaying`, `song`, `playlist`, `titelliste`, `metadata`, `currentsong`)
- Query-Parameter (`k`, `skey`, `key`, `channelkey`)
- API-Hinweise (`ctrl-api`, `metadata/channel`)

Negativfilter:

- Template-URLs mit `${...}`
- irrelevante technische Endpunkte ohne Songbezug

## Stream-Key-Erkennung

Fuer Plattformen mit Kanal-Keys (z. B. Stream-Plattformen) werden Keys generisch extrahiert:

- aus JS-Snippets
- aus JSON-Objekten
- per Matching gegen Station/Stream

Anschliessend werden Kandidat-URLs mit injizierten Key-Parametern erzeugt, z. B.:

- `...getCurrentSong?k=<key>`
- `...getPlaylist?skey=<key>`
- `.../metadata/channel/<key>.json`

## Source-Policy

### 1) Origin-only

Wenn `ORIGIN_ONLY_MODE=True` und `ALLOW_OFFICIAL_CHAIN_SOURCES=False`, akzeptiert das Tool nur:

- Quellen auf bekannten Origin-Basisdomains.

### 2) Origin + offizielle Player-Kette

Wenn `ORIGIN_ONLY_MODE=True` und `ALLOW_OFFICIAL_CHAIN_SOURCES=True`, sind zusaetzlich erlaubt:

- Discovery-Quellen, die aus offiziell geladenen Player-Ressourcen stammen (trusted chain).

Weiterhin ausgeschlossen:

- Verzeichnis-/Aggregator-Domains (z. B. `radio.*`, `radio-assets.com`).

## Parsing von XML/JSON/HTML

### XML

- alle Elemente werden iteriert
- Felder fuer `title`, `artist`, `time`, `duration`, `status` werden gesucht
- Status `now/current/onair/live` erhoeht den Score deutlich

### JSON

- rekursiver Walk durch Objekte/Listen
- extrahiert analoge Feldnamen (`title/song/track`, `artist/author/interpret`, etc.)
- Statusfelder analog zu XML

### HTML

- extrahiert Artist/Title aus semantischen Klassen (z. B. `interpret`, `artist`, `title`, `track`)
- nur fuer URLs mit klaren Reload-/Now-Playing-Hinweisen (z. B. `SSI`, `module`, `box`)

## Frischepruefung der Songdaten

Es gibt zwei Ebenen:

1. Altersgrenze (`MAX_NOWPLAYING_AGE_MINUTES`)
2. Dauerfenster-Pruefung:
   - wenn `starttime` und `duration` vorhanden sind,
   - wird geprueft, ob `jetzt > start + duration + grace`
   - ueberzogene Eintraege werden verworfen

Damit werden veraltete "now"-Eintraege schneller ausgesiebt, ohne sender-spezifischen Code.

## Auswahl des finalen Songs

Pro Poll-Zyklus:

1. ICY-Slot lesen
2. optional Feed-Poll
3. Feed gewinnt, wenn `artist` und `title` eindeutig sind und Quelle erlaubt ist
4. sonst ICY, falls dort klarer Song erkannt wurde

Wichtig:

- Wenn ICY im aktuellen Zyklus keinen verwertbaren Song liefert, wird die Feed-Discovery trotzdem weiter ausgefuehrt.

## Verified-Source-Fastpath (Kodi-Bridge)

Wenn bereits eine verifizierte Quelle fuer den Sender vorliegt, wird diese bevorzugt direkt geprueft:

- Stream-Quelle (`stream_*`) -> ICY-Probe
- Feed-Quelle (`web_feed_*`) -> direkte Feed-Probe (`fetch_now_playing`)

Dadurch werden unnoetige Voll-Discovery-Laeufe reduziert.

## Name-Varianten / station_key-Fallback

Fuer Sender mit leicht variierenden Namensformen kann ein konservativer Name-Fallback greifen
(z. B. Basisname vs. regionale Variante), konfigurierbar in `app/config.py`.

Wichtig:

- Der Fallback ist absichtlich streng (Prefix-Kompatibilitaet + Mindest-Token), um Fehlzuordnungen zu vermeiden.

## Offizielle Player-Config (generisch)

Wenn eine Seite `data-mandate` + `webradio.js` enthaelt, wird zusaetzlich versucht:

1. `.../webradio/<mandate>/config.json` laden
2. passenden Channel (Name/ID/Stream-Match) bestimmen
3. `currentUrl`/`playlistUrl` als Song-Feed-Kandidaten nutzen

## Songwechsel- und Songende-Erkennung

- Songwechsel:
  - neuer Key aus `source_url + artist + title` -> `Song erkannt`
- Kein Spam:
  - gleicher Song wird nicht in jedem Poll erneut als neu geloggt
- Songende:
  - wenn mehrere Poll-Zyklen hintereinander kein eindeutiger Song vorliegt,
  - wird `Songende erkannt` geloggt

## Sendername-Fallback ohne Radio-Browser-Treffer

Wenn Radio-Browser keinen Treffer liefert, prueft das Tool Web-Verzeichnis-Slugs in mehreren generischen Varianten:

- kompakt (`tranceenergyradio`)
- mit Bindestrich (`trance-energy-radio`)
- Varianten ohne Token `radio` (`tranceenergy`, `trance-energy`)

Damit werden reine Webradio-Streams haeufig robuster gefunden, ohne sender-spezifische Regeln.

## Wichtige Grenzen

- Manche Sender liefern waehrend Jingles/Nachrichten absichtlich keine expliziten Artist/Title-Daten.
- Manche Sender aktualisieren externe Feeds verzoegert.
- Ohne eindeutige Tokens in Quelle bleibt nur best-effort.

Der Ansatz bleibt trotzdem robust, weil er mehrere Quellen parallel bewertet und nur eindeutige Daten akzeptiert.
