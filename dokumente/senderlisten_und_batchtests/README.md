# Senderlisten und Batchtests

Dieser Ordner bündelt alle Dateien für Senderlisten, Batchtest-Ergebnisse und externe Namensvergleiche.

## Senderlisten

- `radio_station_list.md`: Masterliste als Tabelle (`Sendername` / `Normalisiert`)
- `radio_station_list_original.txt`: Nur Original-Sendernamen (eine Zeile pro Sender)
- `radio_station_list_normalisiert.txt`: Nur normalisierte Sendernamen (eine Zeile pro Sender)

## Batchtest-Ergebnisse

- `batchtest_result_YYYYMMDD_HHMMSS.tsv`: Ergebnisse aus GUI-Batchläufen

Format:

```tsv
sender\tergebnis\twert
```

- `ergebnis = song` -> `wert = Artist - Title`
- `ergebnis = leer` -> `wert = Grund` (z. B. `missing_field`, `resolve_error:...`)

## Externe Namensvergleiche

- `radio_de_slug_compare.tsv`: Abgleich normalisierter Slugs gegen radio.de
- `tunein_slug_compare.tsv`: Abgleich normalisierter Slugs gegen TuneIn-Suchergebnisse
- `tunein_name_compare.tsv`: Abgleich Originalnamen gegen TuneIn-Suchergebnisse
