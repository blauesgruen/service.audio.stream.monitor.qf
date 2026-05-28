# Changelog

## 2026-05-17

- Now-Playing-Discovery um einen generischen Loverad-/Audalaxy-Adapter erweitert.
- Offizielle Stream-Kataloge und eingebettete Bootstrap-Daten koennen jetzt strukturiert nach `station_id`, `stream_url` und Kanalnamen ausgewertet werden.
- Aus einem sender-sicher gematchten Loverad-Eintrag wird der passende `iris-.../flow.json`-Feed als `trusted`-Kandidat abgeleitet.
- Die Senderzuordnung bleibt generisch: primaer Stream-Match gegen `resolved_url` und `delivery_url`, Name nur als Zusatzsignal.
- Verifiziert am Fall `radio bob! livestream national`: Discovery erzeugt jetzt `https://iris-bob.loverad.io/flow.json?station=69&offset=1&count=1` und liefert daraus ein valides `artist/title`-Paar.

## 2026-05-28

- Now-Playing-Discovery um einen generischen `hr`-Radioplayer-Adapter erweitert.
- Offizielle `hr`-Senderseiten koennen jetzt aus `#hr-radioplayer`-`data-properties` den passenden Push-Kanal (`playlistPush` + `radioStation`) ableiten.
- Daraus entsteht ein strukturierter Kandidat wie `https://push.hr.de/push/<CHANNEL>?version=v2&_qf_hrp=radioplayer_current`; beim Probe-Schritt wird nur `current.artist` + `current.title` aus dem offiziellen WebSocket akzeptiert.
- Dadurch sind `hr1`, `hr2`, `hr3`, `HR Info` und `YOU FM` ueber denselben Providerpfad abgedeckt, ohne sender-spezifische Sonderfaelle.
- Der Stream-Resolver akzeptiert terminale HLS-Media-Playlists jetzt als gueltigen Endpunkt statt sie als unaufloesbare Playlist abzubrechen.
- Die Pair-Validierung erzeugt keine zu aggressiven Kurz-Hints mehr aus Stationsnamen wie `YOU FM`, damit gueltige Titel wie `Need your love` nicht als `generic_pair` verworfen werden.
