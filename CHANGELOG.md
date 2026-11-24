# Changelog

## 2026-11-25

- ASM-QF-Version auf `0.1.13` erhoeht.
- Manuelle Senderzuordnung ("Aktuellen Sender zuordnen"): `collect_program_candidates` liefert den automatisch gewaehlten Treffer immer zuerst und danach alle Programme mit mindestens zwei gemeinsamen Namensbegriffen, sortiert nach Ueberdeckung. Zuvor blieb die Liste bei Namen mit Zusatzwoertern leer ("keine Kandidaten gefunden", z. B. `STAR FM MAXIMUM ROCK! (Berlin)`).

## 2026-11-24

- ASM-QF-Version auf `0.1.12` erhoeht.
- Neue Senderidentifikation (`app/station_identity_trust.py`): Die Kanalkennung wird aus dem Stream-Pfad abgeleitet (Format-/Bitratensegmente wie `mp3-192`, `aac-64`, Playerparameter und Server-Segmente wie `sw282p3` zaehlen nicht). Streams gelten nur bei gleicher Basisdomain und gleichem Kanal als dasselbe Programm; ein gemeinsamer Betreiber oder eine gemeinsame Homepage reicht nicht mehr. Im Zweifel bleiben Kandidaten getrennt.
- Vertrauensstufen: A = eindeutig, B = wahrscheinlich (weitere Programme moeglich), C = ungeklaert. Ein exakter Namenstreffer gilt als eindeutig, solange es keine gleichnamigen Programme oder regionalen Varianten (`STATION_IDENTITY_REGION_TOKENS`) gibt. Stufe B fuehrt eine zusaetzliche, begrenzte Radio-Browser-Abfrage nach regionalen Geschwistern aus.
- Automatisch aufgeloeste Sender werden nicht mehr in `station_uuid_map` gespeichert. Neue Tabelle `station_identity_map` (`source_type`, `source_identifier`, `station_name`, `stationuuid`, `identity_status`, `confirmation_source`, `save_preference`, Zeitstempel); Altzeilen werden einmalig (PRAGMA `user_version`) als `legacy_unverified` uebernommen und nie als bestaetigt behandelt. Die alte Tabelle bleibt erhalten.
- Auswahldialog `qf_selection_dialog.py` (`WindowXMLDialog`, `resources/skins/default/1080i/qf-station-select.xml`): erscheint bei Stufe B/C, laeuft in einem eigenen Thread, fragt pro Wiedergabe nur einmal; Speichern ist standardmaessig aktiv. Abbrechen beendet die Anfrage kontrolliert, ein Senderwechsel verwirft veraltete Dialoge (`app/selection_state.py`).
- Einstellungen `Senderzuordnung` (`manage.py`, Extension `xbmc.python.script`): aktuellen Sender zuordnen, aktuelle Zuordnung anzeigen, gespeicherte Zuordnungen verwalten/loeschen. Eine ausdruecklich bestaetigte Auswahl hat Vorrang vor der Automatik.
- Der Verified-Source-Fastpath liefert keine Quelle aus, die zu einem anderen Programm als der bestaetigten Zuordnung gehoert.
- Neue Logevents: `station_identity_confirmed`, `station_identity_ambiguous`, `station_selection_required/opened/confirmed/cancelled/applied/discarded`, `station_mapping_loaded/saved/invalidated`. Kandidaten-URLs werden ohne Query/Userinfo geloggt.
- Offline-Tests in `tests/` (`python -m unittest discover -s tests -t .`) mit echten Radio-Browser-Antworten.
- Bekannte Grenze: Die Zuordnung haengt noch am Sendernamen; pro PVR-Kanal speichern erfordert eine ASM-Erweiterung (PVR-Kanalkennung im Request).

## 2026-11-23

- ASM-QF-Version auf `0.1.10` erhoeht.
- Bei gleich bewerteten Senderkandidaten mit identischem Namen und identischer Betreiber-Homepage entscheiden `lastcheckok`, `votes` und `bitrate`; bei verschiedenen Betreibern bleibt `station_ambiguous` erhalten.
- Neue Tabelle `station_uuid_map` in `song_data.db` speichert die aufgeloeste Radio-Browser-UUID je Sender-Key und nutzt sie bei Namensanfragen vor der Suche (PVR-Radio ohne Station-ID).
- Bei `station_ambiguous` werden `match_candidates` (Name, UUID, Homepage, Stream-URL, Status, Votes, Bitrate, Evidence) geloggt und im Event `station_lookup_failed` ausgegeben.

## 2026-11-11

- ASM-QF-Version auf `0.1.9` erhoeht.
- Senderkandidaten koennen regelbasiert ueber zusammengesetzte Markennamen und explizite Qualifier wie Klammer-Orte abgeglichen werden.
- Der semantische Fallback waehlt nur einen eindeutig besten Kandidaten; Gleichstaende werden als `station_ambiguous` gemeldet.
- `Response.Meta` dokumentiert `match_method` und `match_evidence`; fuer die Suche wird keine KI verwendet.
- Radio-Browser-Server werden dynamisch ueber `all.api.radio-browser.info` ermittelt statt fest eingetragen.
- „Sender nicht gefunden“ wird von DNS- und allgemeinen Serverfehlern getrennt; die Fehlerklasse steht in `Response.Meta.error_class`.
- Station-ID-Slugs wie `starfmberlin` werden als Namenssuche ausgewertet und nicht als UUID behandelt.

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
