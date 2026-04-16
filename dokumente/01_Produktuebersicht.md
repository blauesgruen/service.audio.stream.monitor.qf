# 01 Produktuebersicht

## Zweck

Das Tool analysiert Internetradio-Streams mit Fokus auf drei Fragen:

1. Was ist die echte Origin-Stream-URL des Senders?
2. Aus welcher Quelle kommen aktuell belastbare Songdaten (`artist` und `title`)?
3. Kann diese Kombination verifiziert und nachvollziehbar gespeichert werden?

## Kernfunktionen

- Sendername -> Stream-URL per Radio-Browser API
- Stream-Aufloesung mit Redirect- und Playlist-Verfolgung (`m3u`, `pls`, `xspf`)
- Trennung von:
  - `Origin URL` (vom Sender verwaltete Quelle)
  - `Delivery URL` (technischer Auslieferungsknoten/CDN)
- ICY-Metadaten-Auslese direkt aus dem Audio-Stream
- Generische Discovery fuer Web-Feeds (XML/JSON/HTML), ohne sender-spezifische Hardcodes
- Discovery bleibt aktiv, auch wenn ICY keinen verwertbaren Songtitel liefert
- Generische Pruefung typischer Icecast/Shoutcast-Status-Endpunkte (`status-json.xsl`, `status.xsl`, `stats`)
- Origin-Only-Modus zur Einschränkung auf Sender-Domains bzw. offiziell verlinkte Zusatz-Domains
- Live-Log in separatem Fenster
- Rohdaten-Detailansicht mit kompletter Nachvollziehbarkeit
- Speicherung verifizierter Quellen in SQLite
- Web-Fallback fuer Sendernamen mit mehreren Slug-Varianten (mit/ohne Bindestrich, mit/ohne `radio`)
- Kodi-Bridge (`ASM-QF`) mit Request/Response-Handshake, typgerechtem Verified-Source-Fastpath (Stream vs Feed) und nachrangigem Ergebnis-Cache
- Bei abweichendem frischem Fastpath-Paar wird ein alter Result-Cache-Hit bewusst uebergangen
- Die Vollkette (Lookup/Resolve/ICY/Discovery) laeuft nur, wenn Fastpath und Result-Cache keinen Treffer liefern
- Deterministischer Abbruchpfad: bei ueberholten Requests schreibt `ASM-QF` eine Response mit `status=aborted`

## Wichtige Begriffe

- `Origin URL`: die aufgeloeste Stream-Quelle vor CDN-/Session-Parametern.
- `Delivery URL`: das konkrete Redirect-Ziel (haeufig mit kurzlebigen Token).
- `Songquelle`: die Quelle, aus der `artist` und `title` final stammen.
- `stream_icy`: Songdaten direkt aus dem Stream-Metadatenkanal.
- `web_feed_xml` / `web_feed_json` / `web_feed_html`: Songdaten aus externer Feed-Quelle.

## Nicht-Ziele

- Keine Audio-Analyse (kein Fingerprinting)
- Keine Garantie, dass jeder Sender maschinenlesbare Songdaten bereitstellt
- Kein sender-spezifischer Hardcode pro Station

## Voraussetzungen

- Python 3.10+ (empfohlen)
- Internetzugriff
- TLS/SSL-Verbindungen sollten funktionieren; bei fehlerhaften Zertifikatsketten wird best-effort auf unverified SSL gewechselt

## Start

```bash
python3 main.py
```

## Ergebnisqualitaet

Die beste Ergebnisqualitaet wird erzielt, wenn der Sender eine explizite Now-Playing-API oder einen XML-Feed mit `artist` und `title` bereitstellt.
