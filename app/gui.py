"""Tkinter GUI for resolving and validating internet radio streams."""

from __future__ import annotations

import json
import threading
import time
import tkinter as tk
from queue import Empty, Queue
from tkinter import messagebox, ttk
from urllib.error import URLError

from .config import (
    ALLOW_OFFICIAL_CHAIN_SOURCES,
    APP_NAME,
    DB_PATH,
    EPG_SEARCH_DEFAULT_ENABLED,
    ORIGIN_ONLY_MODE,
    SONG_REFRESH_INTERVAL_SECONDS,
    UI_POLL_INTERVAL_MS,
)
from .database import SourceDatabase
from .epg_service import EpgService
from .live_logger import LiveLogger
from .metadata import MetadataError, SongMetadataFetcher
from .models import EpgInfo, ResolvedStream, SongInfo, StationMatch
from .now_playing_discovery import NowPlayingDiscoveryService
from .station_lookup import StationLookupError, StationLookupService
from .stream_resolver import StreamResolveError, StreamResolver
from .utils import get_base_domain, is_non_origin_directory_url, is_origin_url, is_probable_url


class RadioToolApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("940x520")

        self.logger = LiveLogger()
        self.db = SourceDatabase(DB_PATH)

        self._results: Queue[tuple[str, object]] = Queue()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None

        self._current_resolved: ResolvedStream | None = None
        self._current_song: SongInfo | None = None
        self._current_epg: EpgInfo | None = None
        self._last_station_match: StationMatch | None = None
        self._feed_candidates: list[str] = []
        self._origin_domains: list[str] = []

        self.url_var = tk.StringVar()
        self.station_var = tk.StringVar(value="-")
        self.resolved_var = tk.StringVar(value="-")
        self.delivery_var = tk.StringVar(value="-")
        self.content_type_var = tk.StringVar(value="-")
        self.song_var = tk.StringVar(value="-")
        self.epg_var = tk.StringVar(value="-")
        self.epg_enabled_var = tk.BooleanVar(value=EPG_SEARCH_DEFAULT_ENABLED)
        self.origin_mode_var = tk.StringVar(
            value=(
                "Origin + offizielle Player-Kette aktiv"
                if ORIGIN_ONLY_MODE and ALLOW_OFFICIAL_CHAIN_SOURCES
                else ("Origin-only aktiv" if ORIGIN_ONLY_MODE else "Origin-only aus")
            )
        )
        self.status_var = tk.StringVar(value="Bereit")

        self._build_ui()
        self._build_log_window()
        self._build_details_window()
        self._schedule_ui_pump()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Sendername oder URL:").grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(frame, text="EPG-Suche aktiv", variable=self.epg_enabled_var).grid(
            row=0, column=4, sticky="e"
        )
        url_entry = ttk.Entry(frame, textvariable=self.url_var, width=105)
        url_entry.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(4, 8))
        url_entry.focus_set()

        self.start_button = ttk.Button(frame, text="Prüfen + Starten", command=self.start_scan)
        self.start_button.grid(row=2, column=0, sticky="w")

        self.stop_button = ttk.Button(frame, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_button.grid(row=2, column=1, sticky="w", padx=(8, 0))

        ttk.Button(frame, text="Live-Log öffnen", command=self.show_log_window).grid(
            row=2, column=2, sticky="w", padx=(8, 0)
        )

        ttk.Button(frame, text="Quell-Details", command=self.show_details_window).grid(
            row=2, column=3, sticky="w", padx=(8, 0)
        )

        self.save_button = ttk.Button(frame, text="Verifiziert speichern", command=self.save_verified, state="disabled")
        self.save_button.grid(row=2, column=4, sticky="e")

        ttk.Separator(frame, orient="horizontal").grid(row=3, column=0, columnspan=5, sticky="ew", pady=10)

        ttk.Label(frame, text="Gefundener Sender:").grid(row=4, column=0, sticky="w")
        ttk.Label(frame, textvariable=self.station_var).grid(row=5, column=0, columnspan=5, sticky="w")

        ttk.Label(frame, text="Original-Stream:").grid(row=6, column=0, sticky="w", pady=(10, 0))
        ttk.Label(frame, textvariable=self.resolved_var, foreground="#0b5").grid(row=7, column=0, columnspan=5, sticky="w")

        ttk.Label(frame, text="Delivery-URL (Redirect-Ziel):").grid(row=8, column=0, sticky="w", pady=(10, 0))
        ttk.Label(frame, textvariable=self.delivery_var).grid(row=9, column=0, columnspan=5, sticky="w")

        ttk.Label(frame, text="Content-Type:").grid(row=10, column=0, sticky="w", pady=(10, 0))
        ttk.Label(frame, textvariable=self.content_type_var).grid(row=11, column=0, sticky="w")

        ttk.Label(frame, text="Aktueller Song:").grid(row=12, column=0, sticky="w", pady=(10, 0))
        ttk.Label(frame, textvariable=self.song_var, font=("TkDefaultFont", 11, "bold")).grid(
            row=13, column=0, columnspan=5, sticky="w"
        )

        ttk.Label(frame, text="EPG-Status:").grid(row=14, column=0, sticky="w", pady=(10, 0))
        ttk.Label(frame, textvariable=self.epg_var).grid(row=15, column=0, columnspan=5, sticky="w")

        ttk.Label(frame, textvariable=self.origin_mode_var).grid(row=16, column=0, columnspan=5, sticky="w", pady=(10, 0))

        ttk.Separator(frame, orient="horizontal").grid(row=17, column=0, columnspan=5, sticky="ew", pady=10)
        ttk.Label(frame, textvariable=self.status_var).grid(row=18, column=0, columnspan=5, sticky="w")

        frame.columnconfigure(0, weight=1)

    def _build_log_window(self) -> None:
        self.log_window = tk.Toplevel(self.root)
        self.log_window.title("Live-Log")
        self.log_window.geometry("980x360")
        self.log_window.protocol("WM_DELETE_WINDOW", self.log_window.withdraw)

        self.log_text = tk.Text(self.log_window, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

        self.log_window.withdraw()

    def _build_details_window(self) -> None:
        self.details_window = tk.Toplevel(self.root)
        self.details_window.title("Quell-Details (Rohdaten)")
        self.details_window.geometry("1100x700")
        self.details_window.protocol("WM_DELETE_WINDOW", self.details_window.withdraw)

        self.details_text = tk.Text(self.details_window, wrap="word", state="disabled")
        self.details_text.pack(fill="both", expand=True)

        self.details_window.withdraw()

    def show_log_window(self) -> None:
        self.log_window.deiconify()
        self.log_window.lift()

    def show_details_window(self) -> None:
        self.details_window.deiconify()
        self.details_window.lift()
        self._render_source_details()

    def _schedule_ui_pump(self) -> None:
        self.logger.drain(self._append_log_line)
        self._consume_results()
        self.root.after(UI_POLL_INTERVAL_MS, self._schedule_ui_pump)

    def _append_log_line(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _consume_results(self) -> None:
        while True:
            try:
                event, payload = self._results.get_nowait()
            except Empty:
                break

            if event == "station":
                self._last_station_match = payload
                self.station_var.set(payload.name or "-")
                self.status_var.set("Sendername aufgelöst")
                self._render_source_details()
            elif event == "resolved":
                resolved = payload
                self._current_resolved = resolved
                self.station_var.set(resolved.station_name or self.station_var.get())
                self.resolved_var.set(resolved.resolved_url)
                self.delivery_var.set(resolved.delivery_url or "-")
                self.content_type_var.set(resolved.content_type or "-")
                self.status_var.set("Stream erfolgreich aufgelöst")
                self._render_source_details()
            elif event == "song":
                song = payload
                self._current_song = song
                display_song = song.stream_title
                if song.artist and song.title:
                    display_song = f"{song.artist} - {song.title}"
                if song.source_kind.startswith("web_feed"):
                    display_song = f"{display_song} [Feed]"
                self.song_var.set(display_song)
                self.status_var.set("Songinfo aktualisiert")
                self.save_button.configure(state="normal")
                self._render_source_details()
            elif event == "epg":
                self._current_epg = payload
                self.epg_var.set(payload.summary)
                self._render_source_details()
            elif event == "epg_disabled":
                self._current_epg = None
                self.epg_var.set("Deaktiviert (GUI-Schalter)")
                self._render_source_details()
            elif event == "feed_candidates":
                self._feed_candidates = payload
                self._render_source_details()
            elif event == "origin_domains":
                self._origin_domains = payload
                self._render_source_details()
            elif event == "error":
                self.status_var.set(str(payload))
            elif event == "done":
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")

    def start_scan(self) -> None:
        value = self.url_var.get().strip()
        if not value:
            messagebox.showwarning("Fehlende Eingabe", "Bitte Sendername oder URL eingeben.")
            return

        if self._worker and self._worker.is_alive():
            messagebox.showinfo("Läuft bereits", "Es läuft bereits eine Überwachung.")
            return

        self._reset_state()
        self.save_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("Prüfe Stream...")

        if self.epg_enabled_var.get():
            self.epg_var.set("Suche aktiv (wartet auf Ergebnis)")
        else:
            self.epg_var.set("Deaktiviert (GUI-Schalter)")

        self._worker = threading.Thread(
            target=self._scan_worker,
            args=(value, bool(self.epg_enabled_var.get())),
            daemon=True,
        )
        self._worker.start()

    def _reset_state(self) -> None:
        self._stop_event.clear()
        self._current_resolved = None
        self._current_song = None
        self._current_epg = None
        self._last_station_match = None
        self._feed_candidates = []
        self._origin_domains = []

        self.station_var.set("-")
        self.resolved_var.set("-")
        self.delivery_var.set("-")
        self.song_var.set("-")
        self.content_type_var.set("-")
        self.epg_var.set("-")
        self._render_source_details()

    def stop_scan(self) -> None:
        self._stop_event.set()
        self.status_var.set("Stop angefordert...")
        self.logger.log("Stop angefordert")

    def save_verified(self) -> None:
        if not self._current_resolved or not self._current_song:
            messagebox.showwarning("Nicht möglich", "Noch keine verifizierten Daten vorhanden.")
            return

        self.db.upsert_verified_source(self._current_resolved, self._current_song, self._current_epg)
        self.status_var.set("Verifizierte Quelle in DB gespeichert")
        self.logger.log("Verifizierte Quelle in DB gespeichert")
        messagebox.showinfo("Gespeichert", "Quelle wurde in der DB gespeichert/aktualisiert.")

    def _scan_worker(self, value: str, epg_enabled: bool) -> None:
        resolver = StreamResolver(self.logger.log)
        fetcher = SongMetadataFetcher(self.logger.log)
        lookup = StationLookupService(self.logger.log)
        epg_service = EpgService(self.logger.log)
        now_playing_discovery = NowPlayingDiscoveryService(self.logger.log)

        try:
            self.logger.log(f"Starte Analyse für: {value}")
            station: StationMatch | None = None
            stream_seed = value

            if not is_probable_url(value):
                self.logger.log(f"Eingabe als Sendername erkannt: {value}")
                station = lookup.find_best_match(value)
                stream_seed = station.stream_url
                self._results.put(("station", station))
                self.logger.log(f"Sendername auf Stream-URL aufgelöst: {stream_seed}")

            resolved = resolver.resolve(stream_seed, original_input=value)
            if station:
                resolved.station_name = station.name

            self.logger.log(f"Original-Stream erkannt: {resolved.resolved_url}")
            if resolved.delivery_url and resolved.delivery_url != resolved.resolved_url:
                self.logger.log(f"Delivery-URL erkannt: {resolved.delivery_url}")
            self._results.put(("resolved", resolved))

            origin_domains = self._collect_origin_domains(station, resolved)
            self._results.put(("origin_domains", sorted(origin_domains)))
            self.logger.log(f"Origin-Domains: {', '.join(sorted(origin_domains)) or '-'}")

            if epg_enabled:
                def epg_probe_worker() -> None:
                    try:
                        epg = epg_service.fetch(
                            resolved.resolved_url,
                            homepage_url=station.homepage if station else "",
                        )
                        self.logger.log(f"EPG-Status: {epg.summary}")
                        self._results.put(("epg", epg))
                    except Exception as epg_err:
                        self.logger.log(f"EPG-Abfrage fehlgeschlagen: {epg_err}")

                threading.Thread(target=epg_probe_worker, daemon=True).start()
            else:
                self.logger.log("EPG-Suche deaktiviert (GUI-Schalter).")
                self._results.put(("epg_disabled", None))

            feed_candidates: list[str] = []
            preferred_feed_url = ""
            reported_no_origin_song = False
            last_song_key = ""
            consecutive_no_song_cycles = 0
            rejected_non_origin_source = False
            restricted_source_mode = ORIGIN_ONLY_MODE or ALLOW_OFFICIAL_CHAIN_SOURCES
            last_stream_error = ""

            def classify_song_source(url: str) -> tuple[bool, str]:
                if not url:
                    return False, ""
                if not ORIGIN_ONLY_MODE:
                    return True, "unrestricted"
                if is_origin_url(url, origin_domains):
                    return True, "origin"
                if (
                    ALLOW_OFFICIAL_CHAIN_SOURCES
                    and now_playing_discovery.is_trusted_candidate(url)
                    and not is_non_origin_directory_url(url)
                ):
                    return True, "official_player_chain"
                return False, "blocked_non_allowed"

            def is_allowed_song_source(url: str) -> bool:
                allowed, _ = classify_song_source(url)
                return allowed

            while not self._stop_event.is_set():
                stream_song: SongInfo | None = None
                chosen_song: SongInfo | None = None

                try:
                    stream_song = fetcher.fetch(resolved.resolved_url)
                    self.logger.log(f"ICY-Slot: {stream_song.stream_title}")
                    last_stream_error = ""
                except (MetadataError, URLError, TimeoutError, OSError) as meta_err:
                    current_error = str(meta_err)
                    if current_error != last_stream_error:
                        self.logger.log(f"Songabfrage fehlgeschlagen: {meta_err}")
                        self._results.put(("error", f"Songabfrage fehlgeschlagen: {meta_err}"))
                        last_stream_error = current_error

                if stream_song:
                    stream_song_is_track = bool(stream_song.artist and stream_song.title)
                    stream_song_is_origin, stream_song_approval = classify_song_source(stream_song.source_url)
                    chosen_song = stream_song if (
                        stream_song_is_track
                        and stream_song_is_origin
                    ) else None
                    if chosen_song:
                        chosen_song.source_approval = stream_song_approval
                    if stream_song_is_track and not stream_song_is_origin:
                        rejected_non_origin_source = True
                        self.logger.log(f"Stream-Metadaten verworfen (nicht erlaubt): {stream_song.source_url}")

                if not feed_candidates:
                    discovered = now_playing_discovery.discover_candidate_urls(
                        resolved=resolved,
                        station=station,
                        stream_headers=stream_song.source_headers if stream_song else {},
                    )
                    feed_candidates = []
                    for url in discovered:
                        if is_allowed_song_source(url):
                            feed_candidates.append(url)
                        else:
                            rejected_non_origin_source = True
                            self.logger.log(f"Feed-Kandidat verworfen (nicht erlaubt): {url}")
                    linked_domains = sorted(now_playing_discovery.get_linked_domains() - origin_domains)
                    if linked_domains:
                        self.logger.log(
                            "Offiziell verlinkte Zusatz-Domains (nicht Origin): " + ", ".join(linked_domains)
                        )
                    self._results.put(("feed_candidates", feed_candidates))

                if feed_candidates:
                    probe_list = [preferred_feed_url] if preferred_feed_url else feed_candidates
                    feed_song = now_playing_discovery.fetch_now_playing(probe_list)
                    if feed_song and feed_song.artist and feed_song.title:
                        # Keep stream headers available in detail view where possible.
                        if stream_song and stream_song.source_headers:
                            feed_song.source_headers = stream_song.source_headers
                        feed_song_is_origin, feed_song_approval = classify_song_source(feed_song.source_url)
                        if feed_song_is_origin:
                            feed_song.source_approval = feed_song_approval
                            chosen_song = feed_song
                            preferred_feed_url = feed_song.source_url or preferred_feed_url
                        else:
                            rejected_non_origin_source = True
                            self.logger.log(
                                f"Feed-Treffer verworfen (nicht erlaubt): {feed_song.source_url}"
                            )

                if chosen_song and chosen_song.artist and chosen_song.title:
                    song_key = "|".join(
                        [
                            chosen_song.source_url or "",
                            chosen_song.artist.strip().lower(),
                            chosen_song.title.strip().lower(),
                        ]
                    )
                    if song_key != last_song_key:
                        approval_label = chosen_song.source_approval or "unclassified"
                        self.logger.log(f"Song erkannt ({approval_label}): {chosen_song.stream_title}")
                        self._results.put(("song", chosen_song))
                        last_song_key = song_key
                    consecutive_no_song_cycles = 0
                    reported_no_origin_song = False
                else:
                    consecutive_no_song_cycles += 1
                    if last_song_key and consecutive_no_song_cycles >= 2:
                        self.logger.log("Songende erkannt: aktuell kein eindeutiger Song (Jingle/Beitrag/Nachrichten)")
                        last_song_key = ""

                    if restricted_source_mode:
                        self.logger.log("Keine Quelle mit eindeutigem Artist in diesem Poll-Zyklus")
                        if not reported_no_origin_song:
                            if ORIGIN_ONLY_MODE and ALLOW_OFFICIAL_CHAIN_SOURCES:
                                message = "Keine aktuelle Origin-/Player-Ketten-Quelle mit eindeutigem Artist gefunden"
                            elif ORIGIN_ONLY_MODE:
                                message = "Keine aktuelle Origin-Quelle mit eindeutigem Artist gefunden"
                            else:
                                message = "Keine aktuelle Quelle mit eindeutigem Artist gefunden"
                            if rejected_non_origin_source:
                                message += " (nicht erlaubte Quelle verworfen)"
                            self._results.put(("error", message))
                            reported_no_origin_song = True

                for _ in range(SONG_REFRESH_INTERVAL_SECONDS):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)

        except (StreamResolveError, StationLookupError, URLError, TimeoutError, OSError) as err:
            self.logger.log(f"Fehler bei Stream-Auflösung: {err}")
            self._results.put(("error", f"Fehler: {err}"))
        except Exception as err:  # pragma: no cover
            self.logger.log(f"Unerwarteter Fehler: {err}")
            self._results.put(("error", f"Unerwarteter Fehler: {err}"))
        finally:
            self._results.put(("done", None))

    def _render_source_details(self) -> None:
        sections = []

        sections.append("### Eingabe/Status")
        sections.append(f"Sendername/URL Eingabe: {self.url_var.get().strip() or '-'}")
        sections.append(f"Aktueller Status: {self.status_var.get()}")
        sections.append(f"EPG-Suche aktiv: {'ja' if self.epg_enabled_var.get() else 'nein'}")
        sections.append("")

        sections.append("### Sender-Lookup (Rohdaten)")
        if self._last_station_match:
            sections.append(f"Match-Name: {self._last_station_match.name}")
            sections.append(f"Stream-Seed: {self._last_station_match.stream_url}")
            sections.append(f"Homepage: {self._last_station_match.homepage or '-'}")
            sections.append("Raw JSON:")
            sections.append(json.dumps(self._last_station_match.raw_record, ensure_ascii=False, indent=2))
        else:
            sections.append("-")
        sections.append("")

        sections.append("### Aufgelöste Stream-Quelle")
        if self._current_resolved:
            sections.append(f"Input: {self._current_resolved.input_url}")
            sections.append(f"Station: {self._current_resolved.station_name or '-'}")
            sections.append(f"Origin URL: {self._current_resolved.resolved_url}")
            sections.append(f"Delivery URL: {self._current_resolved.delivery_url or '-'}")
            sections.append(f"Content-Type: {self._current_resolved.content_type or '-'}")
            sections.append(f"War Playlist: {self._current_resolved.was_playlist}")
        else:
            sections.append("-")
        sections.append("")

        sections.append("### Entdeckte Song-Feed-Quellen")
        if self._feed_candidates:
            for candidate in self._feed_candidates:
                sections.append(candidate)
        else:
            sections.append("-")
        sections.append("")

        sections.append("### Origin-Domains")
        if self._origin_domains:
            sections.extend(self._origin_domains)
        else:
            sections.append("-")
        sections.append("")

        sections.append("### Song-Daten (Rohdaten)")
        if self._current_song:
            sections.append(f"StreamTitle: {self._current_song.stream_title}")
            sections.append(f"Artist: {self._current_song.artist or '-'}")
            sections.append(f"Title: {self._current_song.title or '-'}")
            sections.append(f"Quelle Typ: {self._current_song.source_kind}")
            sections.append(f"Quelle URL: {self._current_song.source_url or '-'}")
            sections.append(f"Quelle Freigabe: {self._current_song.source_approval or '-'}")
            sections.append(f"Raw Metadata Block: {self._current_song.raw_metadata}")
            sections.append("HTTP/ICY Header:")
            sections.append(json.dumps(self._current_song.source_headers, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            sections.append("-")
        sections.append("")

        sections.append("### EPG")
        if self._current_epg:
            sections.append(f"Verfügbar: {self._current_epg.available}")
            sections.append(f"Quelle: {self._current_epg.source_url or '-'}")
            sections.append(f"Summary: {self._current_epg.summary or '-'}")
            if self._current_epg.error:
                sections.append(f"Letzter Fehler: {self._current_epg.error}")
            if self._current_epg.raw_xml:
                sections.append("Raw XML (gekürzt auf 15000 Zeichen):")
                sections.append(self._current_epg.raw_xml[:15000])
        else:
            sections.append("-")

        text = "\n".join(sections)
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", text)
        self.details_text.configure(state="disabled")

    def _collect_origin_domains(
        self,
        station: StationMatch | None,
        resolved: ResolvedStream | None = None,
    ) -> set[str]:
        domains = set()

        if resolved:
            for url_value in (resolved.resolved_url,):
                base = get_base_domain(url_value)
                if base:
                    domains.add(base)

        if not station:
            return domains

        source_type = str(station.raw_record.get("source") or "").strip().lower()
        candidate_urls = [station.stream_url]
        if station.homepage and not is_non_origin_directory_url(station.homepage):
            candidate_urls.append(station.homepage)

        if source_type != "web_directory_fallback":
            for key in ("url", "url_resolved", "homepage", "stream_url"):
                value = station.raw_record.get(key)
                if not isinstance(value, str):
                    continue
                if key == "homepage" and is_non_origin_directory_url(value):
                    continue
                candidate_urls.append(value)

        for value in candidate_urls:
            base = get_base_domain(value)
            if base:
                domains.add(base)

        return domains


def run_app() -> None:
    root = tk.Tk()
    app = RadioToolApp(root)

    def on_close() -> None:
        app.stop_scan()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
