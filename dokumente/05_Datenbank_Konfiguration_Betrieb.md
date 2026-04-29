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

## Kodi-Bridge-DB (ASM-QF)

Pfad:

- `special://userdata/addon_data/service.audio.stream.monitor.qf/song_data.db`

Tabelle:

- `verified_station_sources`

Zweck:

- bevorzugte, verifizierte Quellen fuer den Fastpath in `ASM-QF`
- Lookup primaer ueber `station_key`, optional ueber konservativen Name-Fallback

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

Wichtige QF-Schalter:

- `QF_RESULT_CACHE_ENABLED`, `QF_RESULT_CACHE_TTL_SECONDS`
- `QF_RESULT_CACHE_USE_ONLY_ON_VERIFIED_SOURCE_MISS`
- `QF_FASTPATH_VERIFIED_SOURCE_ENABLED`
- `QF_VERIFIED_SOURCE_FEED_FASTPATH_ENABLED`
- `QF_VERIFIED_SOURCE_FEED_FASTPATH_MAX_SECONDS`
- `QF_VERIFIED_PROBE_MISS_RETURNS_NO_HIT`
- `QF_HOLD_SECONDS`, `QF_HOLD_SECONDS_MAX`
- `QF_STALE_FEED_DROP_SECONDS`
- `QF_REAPPEAR_BLOCK_SECONDS`
- `QF_STATION_KEY_NAME_FALLBACK_ENABLED`
- `QF_STATION_KEY_NAME_FALLBACK_MIN_TOKENS`
- `QF_STATION_KEY_NAME_FALLBACK_MAX_CANDIDATES`
- `QF_SUPERSEDE_PREEMPT_ENABLED`
- `QF_SUPERSEDE_MIDFLIGHT_ENABLED`

## Betriebshinweise

- Sender mit problematischen Zertifikaten koennen dennoch teilweise funktionieren (SSL fallback).
- Delivery-URLs mit Token koennen sich oft aendern; das ist normal.
- Wichtiger als Delivery ist die stabile Origin-/Songquelle.
- Feed-Kandidaten werden einmal aufgebaut und dann bevorzugt abgefragt.
- Bei ueberholten Requests schreibt `ASM-QF` explizit `status=aborted` (kein stiller Abbruchpfad).
- Standardbetrieb: Supersede ist als Preflight aktiv (vor Start der Bearbeitung), Midflight-Supersede ist standardmaessig deaktiviert, um Abbruch-Kaskaden zu vermeiden.
- Request-Ablauf in ASM-QF: erst `verified_source_fastpath`, dann optional `result_cache`; die Vollkette laeuft nur bei Doppel-Miss oder wenn explizit noetig.
- Wenn ein frischer Fastpath-Hit ein anderes `artist/title` liefert als der Cache, wird der Cache bewusst uebergangen (`result_cache_bypassed_pair_changed`).
- Bei Fastpath-Probe-Miss (Feed/ICY liefert aktuell kein gueltiges Paar) wird der Cache ebenfalls uebergangen (`result_cache_bypassed_verified_probe_state`) und optional direkt `no_hit` geliefert.
- Der effektive Hold ist auf `QF_HOLD_SECONDS_MAX` gedeckelt (auch wenn `QF_HOLD_SECONDS` hoeher gesetzt wird).
- Feed-only-Stale-Drops greifen erst nach `QF_STALE_FEED_DROP_SECONDS`, um kurze Statusphasen nicht als Songende zu fehlinterpretieren.
- Nach bestaetigtem Songende blockt ASM-QF ein identisches Wiederauftauchen desselben Paares aus derselben Quelle fuer `QF_REAPPEAR_BLOCK_SECONDS` (Default 600s).

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
- oder dieselben stale Metadaten tauchen spaeter erneut auf; in Kodi blockt die Parity-Schicht ein
  identisches Wiederauftauchen deshalb fuer die konfigurierte Sperrzeit

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
