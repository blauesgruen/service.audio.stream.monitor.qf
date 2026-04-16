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
- Das Runtime-Label-Property `RadioMonitor.QF.Response.StationUsed` (effektiv verwendeter Sender) ist dort ebenfalls beschrieben.
  - Verhalten: bleibt waehrend `pending` auf dem letzten terminalen Wert und wird erst bei terminaler Response aktualisiert.
- Die aktuelle Request-Reihenfolge in ASM-QF lautet: `verified_source_fastpath` -> `result_cache` -> Vollkette; bei Paarwechsel kann Cache bewusst uebergangen werden (`result_cache_bypassed_pair_changed`).
- Die aktuellen Parity-Stabilitaetsregeln (`QF_HOLD_SECONDS_MAX`, `QF_STALE_FEED_DROP_SECONDS`) sind in `04_...`, `05_...` und `06_...` beschrieben.

## Schnellstart

```bash
python3 main.py
```

## Ziel des Tools in einem Satz

Aus einem Sendernamen oder einer URL die echte Origin-Streamquelle aufloesen, auch ohne verwertbare ICY-Slot-Daten die aktuell gueltige Songquelle mit eindeutigen Feldern (`artist`/`title`) finden, alles transparent loggen und verifizierte Ergebnisse in SQLite speichern.
