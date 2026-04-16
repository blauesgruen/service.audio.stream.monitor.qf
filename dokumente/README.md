# Dokumentation: Radio Source Finder

Diese Dokumentation beschreibt das Tool vollstaendig aus Anwendersicht und aus technischer Sicht.

## Inhalt

1. [Produktuebersicht](./01_Produktuebersicht.md)
2. [Bedienung der GUI](./02_Bedienung_GUI.md)
3. [Architektur und Datenfluss](./03_Architektur_Datenfluss.md)
4. [Now-Playing-Discovery und Origin-Regeln](./04_NowPlaying_und_Origin_Regeln.md)
5. [Datenbank, Konfiguration und Betrieb](./05_Datenbank_Konfiguration_Betrieb.md)
6. [Entwicklerdokumentation](./06_Entwicklerdokumentation.md)
7. [Senderlisten und Batchtests](./senderlisten_und_batchtests/README.md)

Hinweis:

- Der Kodi-Bridge-Contract (`ASM <-> ASM-QF`, inkl. Pflicht-Response auch bei `aborted/superseded`) ist in `06_Entwicklerdokumentation.md` dokumentiert.
- ASM-QF liefert den effektiv verwendeten Sender in `RadioMonitor.QF.Response.Meta.station_used`; ASM setzt daraus sein eigenes Label (ASM-Namespace).
- Die aktuelle Request-Reihenfolge in ASM-QF lautet: `verified_source_fastpath` -> (optional) `result_cache` -> Vollkette.
  - `result_cache` wird nur als Fallback bei echtem `verified_source`-Miss genutzt.
  - Bei Fastpath-Probe-Miss (Feed/ICY ohne gueltiges Paar) wird der Cache uebergangen (`result_cache_bypassed_verified_probe_state`).
- Die aktuellen Parity-Stabilitaetsregeln (`QF_HOLD_SECONDS_MAX`, `QF_STALE_FEED_DROP_SECONDS`) sind in `04_...`, `05_...` und `06_...` beschrieben.

## Schnellstart

```bash
python3 main.py
```

## Ziel des Tools in einem Satz

Aus einem Sendernamen oder einer URL die echte Origin-Streamquelle aufloesen, auch ohne verwertbare ICY-Slot-Daten die aktuell gueltige Songquelle mit eindeutigen Feldern (`artist`/`title`) finden, alles transparent loggen und verifizierte Ergebnisse in SQLite speichern.
