# Changelog

## 2026-05-17

- Now-Playing-Discovery um einen generischen Loverad-/Audalaxy-Adapter erweitert.
- Offizielle Stream-Kataloge und eingebettete Bootstrap-Daten koennen jetzt strukturiert nach `station_id`, `stream_url` und Kanalnamen ausgewertet werden.
- Aus einem sender-sicher gematchten Loverad-Eintrag wird der passende `iris-.../flow.json`-Feed als `trusted`-Kandidat abgeleitet.
- Die Senderzuordnung bleibt generisch: primaer Stream-Match gegen `resolved_url` und `delivery_url`, Name nur als Zusatzsignal.
- Verifiziert am Fall `radio bob! livestream national`: Discovery erzeugt jetzt `https://iris-bob.loverad.io/flow.json?station=69&offset=1&count=1` und liefert daraus ein valides `artist/title`-Paar.
