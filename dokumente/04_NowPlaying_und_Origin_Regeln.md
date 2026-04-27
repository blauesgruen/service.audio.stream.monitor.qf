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
- verschachtelte Script-Bundles auf derselben Basisdomain, wenn dort Feed- oder weitere Script-URLs dynamisch zusammengesetzt werden

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

## Kandidaten-Priorisierung vor dem Polling

Nach dem Ranking werden Kandidaten fuer den Abruf nochmals zentral priorisiert:

1. Offizielle HTML-Now-Playing-Kandidaten (domainnah zum Sender)
2. Starke strukturierte Feed-URLs (vor allem `XML`/`JSON`)
3. Restliche Kandidaten

Damit bleiben klassische Player-Seiten sichtbar, ohne strukturierte Feeds zu verdraengen.

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
- bevorzugt bei Listen-Feeds aktive Eintraege ueber Zeitfenster (`starttime + duration`) und Zustandsfelder wie `playingMode` statt blind den ersten Listeneintrag zu nehmen

### HTML

- extrahiert Artist/Title aus semantischen Klassen (z. B. `interpret`, `artist`, `title`, `track`)
- nur fuer URLs mit klaren Reload-/Now-Playing-Hinweisen (z. B. `SSI`, `module`, `box`)

## Frischepruefung der Songdaten

Es gibt zwei Ebenen:

1. Altersgrenze (`MAX_NOWPLAYING_AGE_MINUTES`)
2. Dauerfenster-Pruefung:
   - wenn `starttime` und `duration` vorhanden sind,
   - wird geprueft, ob der Eintrag aktuell aktiv, schon abgelaufen oder noch zukuenftig ist
   - ueberzogene Eintraege werden verworfen
   - aktive Eintraege werden bei JSON-Listen deutlich bevorzugt

Damit werden veraltete "now"-Eintraege schneller ausgesiebt, ohne sender-spezifischen Code.

## Auswahl des finalen Songs

Pro Poll-Zyklus:

1. ICY-Slot lesen
2. optional Feed-Poll
3. Feed gewinnt, wenn `artist` und `title` eindeutig sind und Quelle erlaubt ist
4. sonst ICY, falls dort klarer Song erkannt wurde

Wichtig:

- Wenn ICY im aktuellen Zyklus keinen verwertbaren Song liefert, wird die Feed-Discovery trotzdem weiter ausgefuehrt.

## Paralleles Feed-Probing

`fetch_now_playing` kann Kandidaten parallel in Batches pruefen.

- Konfiguration ueber:
  - `NOWPLAYING_PARALLEL_PROBING_ENABLED`
  - `NOWPLAYING_PARALLEL_MAX_WORKERS`
  - `NOWPLAYING_PARALLEL_BATCH_SIZE`
- Trefferreihenfolge bleibt stabil zur priorisierten Kandidatenliste.
- Bei erstem gueltigen `artist/title`-Treffer wird der Poll-Zyklus frueh beendet.

## Verified-Source-Fastpath (Kodi-Bridge)

Wenn bereits eine verifizierte Quelle fuer den Sender vorliegt, wird diese bevorzugt direkt geprueft:

- Stream-Quelle (`stream_*`) -> ICY-Probe
- Feed-Quelle (`web_feed_*`) -> direkte Feed-Probe (`fetch_now_playing`)

Wichtig fuer Fastpath-Bewertung:

- Feed- und Stream-Treffer koennen den finalen Hit liefern, Stream-Fastpath hat aber zusaetzliche Schutzregeln.
- Stream-Fastpath erfordert konfigurierbar:
  - Mindest-Confidence (`QF_VERIFIED_SOURCE_STREAM_FASTPATH_MIN_CONFIDENCE`)
  - optional bestaetigte Verifikation (`QF_VERIFIED_SOURCE_STREAM_FASTPATH_REQUIRE_CONFIRMED`)
- Wenn eine verifizierte Quelle geprobt wurde, aber aktuell kein gueltiges Paar liefert, wird ein alter `result_cache`-Treffer nicht blind weiterverwendet.
- In diesem Fall kann der Fastpath direkt `no_hit` liefern (`verified_fastpath_probe_only`), damit Songwechsel nicht durch stale Cache-Hits maskiert werden.

Dadurch werden unnoetige Voll-Discovery-Laeufe reduziert.

## Verified-Source-Persistenz (Feed vor Stream)

Beim Speichern verifizierter Quellen gilt:

- Feed-Quellen werden mit hoher Confidence gespeichert.
- Stream-Quellen werden erst nach wiederholter gleicher Paar-Bestaetigung gespeichert
  (`QF_VERIFIED_SOURCE_STREAM_CONFIRM_HITS` innerhalb `QF_VERIFIED_SOURCE_STREAM_CONFIRM_WINDOW_SECONDS`).
- Optional wird Stream-Persistenz unterdrueckt, wenn bereits eine Feed-Quelle fuer die Station bevorzugt ist
  (`QF_VERIFIED_SOURCE_STREAM_SKIP_IF_FEED_PRESENT`).

## QF-Parity gegen Flackern (Kodi)

Die Kodi-Bridge (`ASM-QF`) nutzt zusaetzlich eine Parity-Schicht fuer stabile Entscheidungen:

- `QF_HOLD_SECONDS` wird durch `QF_HOLD_SECONDS_MAX` begrenzt (aktuell max. 3.0s).
- Ein schwacher Feed-only-Hit (`web_feed_*` + fehlendes klares Stream-Signal) wird nicht sofort
  in `no_hit` abgewertet, sondern erst nach `QF_STALE_FEED_DROP_SECONDS` (konservativ, aktuell 180s).
- Ziel: kurze Jingle-/Status-Phasen ueberbruecken, ohne echte Songwechsel dauerhaft zu maskieren.

Wichtig:

- Songende bleibt priorisiert: bei bestaetigtem `no_hit` wird der letzte Songzustand beendet.
- Das reduziert `hit -> no_hit -> hit`-Pendeln bei verzoegerten Feed-/ICY-Zyklen.

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
4. falls ein Player seine Feed-URL erst in Script-Bundles zusammensetzt, werden relative Script- und Feed-Pfade derselben Domain nachverfolgt

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



