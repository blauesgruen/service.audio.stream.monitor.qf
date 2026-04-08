# 05 Datenbank, Konfiguration und Betrieb

## SQLite-Datenbank

Pfad:

- `radio_sources.db` im Projekt-Root

Tabelle:

- `verified_sources`

Wesentliche Spalten:

- `input_url`
- `station_name`
- `resolved_url`
- `delivery_url`
- `content_type`
- `was_playlist`
- `last_stream_title`
- `last_artist`
- `last_title`
- `song_source_kind`
- `song_source_url`
- `raw_metadata`
- `source_headers`
- `epg_available`
- `epg_source_url`
- `epg_summary`
- `verified_at`

Unique-Key:

- `(input_url, resolved_url)`

Speichern ist ein Upsert: vorhandene Eintraege werden aktualisiert, nicht dupliziert.

## Beispielabfrage

```sql
SELECT
  station_name,
  resolved_url,
  song_source_kind,
  song_source_url,
  last_artist,
  last_title,
  verified_at
FROM verified_sources
ORDER BY verified_at DESC;
```

## Konfigurationsparameter (`app/config.py`)

Wichtige Schalter:

- `ORIGIN_ONLY_MODE`
  - `True`: nur Origin-Domain oder trusted, offiziell verlinkte Zusatz-Domain
- `SONG_REFRESH_INTERVAL_SECONDS`
  - Polling-Intervall fuer Songupdates
- `REQUEST_TIMEOUT_SECONDS`
  - Netzwerk-Timeout Stream/Lookup
- `DISCOVERY_REQUEST_TIMEOUT_SECONDS`
  - Timeout pro Discovery-Request
- `DISCOVERY_MAX_CANDIDATES`
  - Begrenzung der Feed-Kandidaten
- `MAX_NOWPLAYING_AGE_MINUTES`
  - harte Altersgrenze fuer Feed-Eintraege
- `NOWPLAYING_DURATION_GRACE_SECONDS`
  - Toleranz ueber `start + duration` hinaus
- `EPG_REQUEST_TIMEOUT_SECONDS`
  - Timeout EPG-Probe

## Betriebshinweise

- Sender mit problematischen Zertifikaten koennen dennoch teilweise funktionieren (SSL fallback).
- Delivery-URLs mit Token koennen sich oft aendern; das ist normal.
- Wichtiger als Delivery ist die stabile Origin-/Songquelle.
- Feed-Kandidaten werden einmal aufgebaut und dann bevorzugt abgefragt.

## Troubleshooting

### 1) "Fehler bei Stream-Aufloesung" mit Zertifikatsfehler

Moeglich:

- lokale CA-Kette unvollstaendig
- Server mit ungueltigem Zertifikat

Ist teils abgefangen; falls nicht, Netzwerk/SSL lokal pruefen.

### 2) Nur ICY-Slot, aber kein Artist/Title

Bedeutet:

- Stream liefert nur Moderations-/Jingle-Text
- kein valider Trennmarker erkannt

Dann muss die Discovery einen externen Feed finden. Das Tool macht diese Feed-Suche auch dann weiter, wenn ICY im Zyklus fehlschlaegt.

### 3) Feed gefunden, aber veraltet

Das Tool verwirft alte Eintraege ueber:

- globale Altersgrenze
- und (wenn vorhanden) `starttime + duration + grace`

Wenn trotzdem veraltet:

- Feed liefert selbst veraltete Daten
- oder relevante Felder sind nicht vorhanden

### 4) "Keine Quelle mit eindeutigem Artist"

Bedeutet:

- im aktuellen Poll-Zyklus keine Quelle mit klaren `artist/title`
- kann bei Nachrichten/Jingles normal sein

### 5) EPG nicht verfuegbar

Normalfall bei vielen Sendern.

Das Tool prueft nur wenige standardisierte SPI/EPG-Pfade (best-effort), mit Probe-Limit.

### 6) Sendername wird nicht gefunden

Wenn Radio-Browser leer ist, nutzt das Tool einen Web-Fallback (z. B. `radio.de`/`radio.net`) mit mehreren Slug-Varianten (mit/ohne Bindestrich, mit/ohne `radio`).

## Wartung und Erweiterung

Empfohlene Erweiterungen:

1. Optionale Exportfunktion der `verified_sources` als CSV/JSON.
2. Optionaler CLI-Modus fuer headless Monitoring.
3. Historisierung pro Songwechsel (separate Tabelle), falls Verlauf gewuenscht.
4. Optionales Health-Scoring pro Quelle (Trefferquote, Latenz, Frische).
