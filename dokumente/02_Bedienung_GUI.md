# 02 Bedienung der GUI

## Hauptfenster

Eingabefeld: `Sendername oder URL`

Buttons:

- `Pruefen + Starten`: startet Aufloesung und Polling
- `Stop`: stoppt den laufenden Worker
- `Live-Log oeffnen`: zeigt fortlaufende technische Schritte
- `Quell-Details`: zeigt Rohdaten (Lookup, Header, Feed-Rohdaten, EPG)
- `Verifiziert speichern`: schreibt den aktuellen verifizierten Zustand in SQLite

Statusfelder:

- `Gefundener Sender`
- `Original-Stream`
- `Delivery-URL`
- `Content-Type`
- `Aktueller Song`
- `EPG-Status`
- globaler Status unten

## Typischer Ablauf

1. Sendername eingeben, z. B. `mdr jump`.
2. `Pruefen + Starten` klicken.
3. Tool sucht Sender-Match (Radio-Browser) und loest Stream auf.
4. Tool liest ICY-Metadaten und startet parallel EPG-Probe.
5. Tool sucht automatisch moegliche Web-Feeds fuer klare Songdaten (`artist/title`), auch wenn ICY keinen Songtitel liefert.
6. Bei Treffer wird Songanzeige aktualisiert.
7. Bei passendem Zustand `Verifiziert speichern` ausfuehren.

## Was in der Songzeile angezeigt wird

- `Artist - Title`: sauberer Treffer
- Suffix `[Feed]`: Song kommt aus XML/JSON-Feed statt direkt aus ICY
- `-` oder Fehlermeldung: aktuell kein eindeutiges `artist/title` verfuegbar

## Live-Log lesen

Jede Zeile hat Zeitstempel: `[YYYY-MM-DD HH:MM:SS] ...`

Wichtige Meldungen:

- `Sender-Match`: bester Sender wurde gefunden
- `Resolve step N`: URL-Aufloesungsschritt
- `Original-Stream erkannt`: Origin URL steht fest
- `Delivery-URL erkannt`: Redirect-Ziel
- `ICY metaint erkannt`: Stream liefert ICY-Metadaten
- `Now-Playing Kandidaten gefunden`: Discovery hat Feed-Quellen gefunden
- `Now-Playing Treffer aus Feed`: konkrete Quelle mit Daten wurde genutzt
- `Song erkannt`: neuer Songwechsel erkannt
- `Songende erkannt`: aktuell kein klarer Song mehr (z. B. Jingle/Beitrag)
- `Songabfrage fehlgeschlagen`: ICY aktuell nicht verwertbar; Discovery laeuft trotzdem weiter
- `ASM-QF Request gesendet`: Request an die Kodi-Bridge wurde geschrieben
- `event=request_result ... status=aborted`: ueberholter Request wurde deterministisch beendet
- `event=result_cache_hit` / `reason=verified_source_fastpath`: schneller Treffer ohne volle Aufloesungskette

## Quell-Details-Fenster: Sektionen

- `Eingabe/Status`
- `Sender-Lookup (Rohdaten)` inkl. Radio-Browser-JSON
- `Aufgeloeste Stream-Quelle` (Origin/Delivery)
- `Entdeckte Song-Feed-Quellen`
- `Origin-Domains`
- `Song-Daten (Rohdaten)` inkl. `Quelle Typ`, `Quelle URL`, Rohblock
- zusaetzlich `Quelle Freigabe` (`origin` oder `official_player_chain`)
- `EPG`

Damit ist nachvollziehbar, aus welcher konkreten URL ein Song stammt.

## Stop/Neustart

- `Stop` setzt ein Stop-Event und beendet den Polling-Loop sauber.
- Ein neuer Start resetet den UI-Zustand und beginnt komplett neu.

## Verifiziert speichern

Gespeichert wird der aktuelle Snapshot aus:

- Stream-Aufloesung
- Songdaten inkl. Quelltyp/Quell-URL
- Headern
- EPG-Status

Ein vorhandener Datensatz mit gleicher `(input_url, resolved_url)` wird aktualisiert.
