import json
import os
import re
import sqlite3
import sys
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs


WINDOW = xbmcgui.Window(10000)

REQ_ID = "RadioMonitor.QF.Request.Id"
REQ_STATION = "RadioMonitor.QF.Request.Station"
REQ_STATION_ID = "RadioMonitor.QF.Request.StationId"
REQ_MODE = "RadioMonitor.QF.Request.Mode"
REQ_TS = "RadioMonitor.QF.Request.Ts"

RES_ID = "RadioMonitor.QF.Response.Id"
RES_STATUS = "RadioMonitor.QF.Response.Status"
RES_ARTIST = "RadioMonitor.QF.Response.Artist"
RES_TITLE = "RadioMonitor.QF.Response.Title"
RES_SOURCE = "RadioMonitor.QF.Response.Source"
RES_REASON = "RadioMonitor.QF.Response.Reason"
RES_META = "RadioMonitor.QF.Response.Meta"
RES_TS = "RadioMonitor.QF.Response.Ts"

QF_ADDON_ID = "service.audio.stream.monitor.qf"
QF_VERIFIED_SOURCE_KIND = "qf_verified"


def _log(message, level=xbmc.LOGINFO):
    xbmc.log(f"[ASM-QF] {message}", level)


def _translate_path(path_value):
    value = str(path_value or "")
    if not value:
        return ""
    try:
        return xbmcvfs.translatePath(value)
    except Exception:
        try:
            return xbmc.translatePath(value)
        except Exception:
            return value


class QFLogger:
    def _serialize_fields(self, fields):
        parts = []
        for key, value in fields.items():
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            parts.append(f"{key}={text}")
        return " ".join(parts)

    def emit(self, event, level=xbmc.LOGINFO, message="", **fields):
        event_name = str(event or "").strip() or "event"
        field_text = self._serialize_fields(fields)
        chunks = [f"event={event_name}"]
        if message:
            chunks.append(str(message))
        if field_text:
            chunks.append(field_text)
        _log(" | ".join(chunks), level=level)

    def debug(self, event, message="", **fields):
        self.emit(event=event, level=xbmc.LOGDEBUG, message=message, **fields)

    def info(self, event, message="", **fields):
        self.emit(event=event, level=xbmc.LOGINFO, message=message, **fields)

    def warning(self, event, message="", **fields):
        self.emit(event=event, level=xbmc.LOGWARNING, message=message, **fields)

    def error(self, event, message="", **fields):
        self.emit(event=event, level=xbmc.LOGERROR, message=message, **fields)


class QFBridgeService(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.addon = xbmcaddon.Addon()
        self.logger = QFLogger()
        self.last_request_id = ""
        self._imports_ready = False
        self._import_root = ""
        self._import_error = ""
        self._resolution_cache: dict[str, dict] = {}
        self._resolution_cache_ttl_seconds = 180

    def _get_setting_bool(self, key, default=False):
        try:
            return self.addon.getSettingBool(key)
        except Exception:
            raw = (self.addon.getSetting(key) or "").strip().lower()
            if raw in {"true", "1", "yes", "on"}:
                return True
            if raw in {"false", "0", "no", "off"}:
                return False
            return bool(default)

    def _get_project_root(self):
        root = _translate_path(self.addon.getAddonInfo("path") or "").strip()
        return root

    def _ensure_imports(self):
        root = self._get_project_root()
        if not root:
            self._imports_ready = False
            self._import_error = "Addon-Pfad ist leer"
            return False
        if not os.path.isdir(root):
            self._imports_ready = False
            self._import_error = f"Addon-Pfad nicht gefunden: {root}"
            return False
        if not os.path.isdir(os.path.join(root, "app")):
            self._imports_ready = False
            self._import_error = f"Addon ist nicht self-contained (app/ fehlt): {root}"
            return False

        if self._imports_ready and self._import_root == root:
            return True

        if root not in sys.path:
            sys.path.insert(0, root)

        try:
            # Import lazily so addon can stay installed even without active bridge.
            from app.config import ALLOW_OFFICIAL_CHAIN_SOURCES, ORIGIN_ONLY_MODE
            from app.metadata import SongMetadataFetcher
            from app.now_playing_discovery import NowPlayingDiscoveryService
            from app.song_validation import prefilter_pair
            from app.station_lookup import StationLookupService
            from app.stream_resolver import StreamResolver
            from app.utils import (
                get_base_domain,
                is_non_origin_directory_url,
                is_origin_url,
                is_probable_url,
            )
        except Exception as err:
            self._imports_ready = False
            self._import_error = str(err)
            return False

        self._imports_ready = True
        self._import_root = root
        self._import_error = ""
        self.ALLOW_OFFICIAL_CHAIN_SOURCES = ALLOW_OFFICIAL_CHAIN_SOURCES
        self.ORIGIN_ONLY_MODE = ORIGIN_ONLY_MODE
        self.SongMetadataFetcher = SongMetadataFetcher
        self.NowPlayingDiscoveryService = NowPlayingDiscoveryService
        self.prefilter_pair = prefilter_pair
        self.StationLookupService = StationLookupService
        self.StreamResolver = StreamResolver
        self.get_base_domain = get_base_domain
        self.is_non_origin_directory_url = is_non_origin_directory_url
        self.is_origin_url = is_origin_url
        self.is_probable_url = is_probable_url
        return True

    def _set_property(self, key, value):
        WINDOW.setProperty(key, "" if value is None else str(value))

    def _clear_response(self):
        self._set_property(RES_ID, "")
        self._set_property(RES_STATUS, "")
        self._set_property(RES_ARTIST, "")
        self._set_property(RES_TITLE, "")
        self._set_property(RES_SOURCE, "")
        self._set_property(RES_REASON, "")
        self._set_property(RES_META, "")
        self._set_property(RES_TS, "")

    def _write_response(self, req_id, status, artist="", title="", source="", reason="", meta=None):
        self._clear_response()
        self._set_property(RES_ID, req_id)
        self._set_property(RES_STATUS, status)
        self._set_property(RES_ARTIST, artist)
        self._set_property(RES_TITLE, title)
        self._set_property(RES_SOURCE, source)
        self._set_property(RES_REASON, reason)
        if meta:
            self._set_property(RES_META, json.dumps(meta, ensure_ascii=False))
        self._set_property(RES_TS, str(int(time.time())))

    def _collect_origin_domains(self, station, resolved):
        domains = set()
        if resolved:
            base = self.get_base_domain(resolved.resolved_url)
            if base:
                domains.add(base)

        if not station:
            return domains

        source_type = str(station.raw_record.get("source") or "").strip().lower()
        candidate_urls = [station.stream_url]
        if station.homepage and not self.is_non_origin_directory_url(station.homepage):
            candidate_urls.append(station.homepage)

        if source_type != "web_directory_fallback":
            for key in ("url", "url_resolved", "homepage", "stream_url"):
                value = station.raw_record.get(key)
                if not isinstance(value, str):
                    continue
                if key == "homepage" and self.is_non_origin_directory_url(value):
                    continue
                candidate_urls.append(value)

        for value in candidate_urls:
            base = self.get_base_domain(value)
            if base:
                domains.add(base)

        return domains

    def _normalize_station_name(self, value):
        return " ".join(str(value or "").strip().lower().split())

    def _normalize_url(self, value):
        return str(value or "").strip().lower()

    def _normalize_station_id(self, value):
        text = str(value or "")
        if not text:
            return ""
        text = re.sub(r"\[/?COLOR[^\]]*\]", " ", text, flags=re.IGNORECASE)
        text = text.replace("•", " ")
        text = " ".join(text.strip().lower().split())
        if text.startswith("stationid:"):
            text = text[len("stationid:") :].strip()
        return text

    def _sanitize_station_text(self, value):
        text = str(value or "")
        if not text:
            return ""
        text = re.sub(r"\[/?COLOR[^\]]*\]", " ", text, flags=re.IGNORECASE)
        text = text.replace("•", " ")
        text = " ".join(text.strip().split())
        return text

    def _compact_station_text(self, value):
        text = self._sanitize_station_text(value).lower()
        if not text:
            return ""
        for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
            text = text.replace(src, dst)
        text = re.sub(r"[^a-z0-9]+", "", text)
        return text

    def _build_station_lookup_variants(self, value):
        raw = self._sanitize_station_text(value)
        if not raw:
            return []

        variants = []
        seen = set()

        def add(candidate):
            clean = " ".join(str(candidate or "").strip().split())
            if not clean:
                return
            key = clean.lower()
            if key in seen:
                return
            seen.add(key)
            variants.append(clean)

        add(raw)
        add(re.sub(r"[-_./|]+", " ", raw))
        compact = self._compact_station_text(raw)
        if compact and len(compact) >= 3:
            add(compact)

        return variants

    def _find_station_by_name_with_fallback(self, lookup, station_input, station_id_norm=""):
        variants = self._build_station_lookup_variants(station_input)
        if not variants:
            raise ValueError("Kein gültiger Sendername für Lookup vorhanden.")

        last_error = None
        for idx, variant in enumerate(variants):
            try:
                if station_id_norm:
                    station = lookup.find_best_match(variant, station_id=station_id_norm)
                else:
                    station = lookup.find_best_match(variant)
                if idx > 0:
                    self.logger.info(
                        "station_name_lookup_fallback_ok",
                        input=station_input,
                        variant=variant,
                        station_id=station_id_norm,
                        name=station.name,
                    )
                return station
            except Exception as err:
                last_error = err
                self.logger.debug(
                    "station_name_lookup_variant_failed",
                    input=station_input,
                    variant=variant,
                    station_id=station_id_norm,
                    error=str(err),
                )

        raise last_error if last_error else ValueError("Kein passender Sender gefunden.")

    def _build_station_key(self, station_name, station_id=""):
        station_id_norm = self._normalize_station_id(station_id)
        if station_id_norm:
            return f"stationid:{station_id_norm}"
        name_norm = self._normalize_station_name(station_name)
        if not name_norm:
            return ""
        return f"name:{name_norm}"

    def _build_resolution_cache_key(self, station_input, station_id=""):
        station_id_norm = self._normalize_station_id(station_id)
        if station_id_norm:
            return f"stationid:{station_id_norm}"
        text = str(station_input or "").strip()
        if not text:
            return ""
        if self.is_probable_url(text):
            return f"url:{self._normalize_url(text)}"
        return f"name:{self._normalize_station_name(text)}"

    def _get_cached_resolution(self, cache_key):
        if not cache_key:
            return None
        item = self._resolution_cache.get(cache_key)
        if not item:
            return None
        age = time.time() - float(item.get("ts") or 0.0)
        if age > float(self._resolution_cache_ttl_seconds):
            self._resolution_cache.pop(cache_key, None)
            return None
        return item

    def _store_cached_resolution(self, cache_key, station, resolved, origin_domains):
        if not cache_key or not resolved:
            return
        self._resolution_cache[cache_key] = {
            "ts": time.time(),
            "station": station,
            "resolved": resolved,
            "origin_domains": sorted(origin_domains or []),
        }
        # Keep cache bounded for long-running Kodi sessions.
        if len(self._resolution_cache) > 64:
            oldest_key = min(
                self._resolution_cache,
                key=lambda key: float(self._resolution_cache[key].get("ts") or 0.0),
            )
            self._resolution_cache.pop(oldest_key, None)

    def _get_qf_db_path(self):
        addon_id = str(self.addon.getAddonInfo("id") or "").strip() or QF_ADDON_ID
        return _translate_path(
            f"special://userdata/addon_data/{addon_id}/song_data.db"
        )

    def _ensure_verified_sources_schema(self, conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_station_sources (
                station_key       TEXT NOT NULL,
                station_name      TEXT NOT NULL DEFAULT '',
                station_name_norm TEXT NOT NULL DEFAULT '',
                source_url        TEXT NOT NULL,
                source_url_norm   TEXT NOT NULL,
                source_kind       TEXT NOT NULL DEFAULT 'stream',
                verified_by       TEXT NOT NULL DEFAULT '',
                confidence        REAL NOT NULL DEFAULT 1.0,
                verified_at_utc   TEXT NOT NULL DEFAULT '',
                last_seen_ts      INTEGER NOT NULL DEFAULT 0,
                meta_json         TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (station_key, source_url_norm)
            );
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_verified_sources_url_norm
            ON verified_station_sources(source_url_norm);
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_verified_sources_station_norm
            ON verified_station_sources(station_name_norm);
            """
        )

    def _record_verified_source(
        self,
        station_name,
        source_url,
        confidence=0.95,
        meta=None,
        station_id="",
    ):
        station_key = self._build_station_key(station_name, station_id=station_id)
        station_name = str(station_name or "").strip()
        station_name_norm = self._normalize_station_name(station_name)
        source_url = str(source_url or "").strip()
        source_url_norm = self._normalize_url(source_url)
        if not station_key or not source_url or not source_url_norm:
            return False

        db_path = self._get_qf_db_path()
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.isdir(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        confidence = max(0.0, min(1.0, float(confidence)))
        last_seen_ts = int(time.time())
        verified_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_seen_ts))
        meta_json = ""
        if meta:
            try:
                meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                meta_json = ""
        if not meta_json:
            meta_json = json.dumps({"station_key": station_key}, ensure_ascii=False, separators=(",", ":"))

        try:
            conn = sqlite3.connect(db_path, timeout=2.0)
            try:
                self._ensure_verified_sources_schema(conn)
                conn.execute(
                    """
                    INSERT INTO verified_station_sources (
                        station_key,
                        station_name,
                        station_name_norm,
                        source_url,
                        source_url_norm,
                        source_kind,
                        verified_by,
                        confidence,
                        verified_at_utc,
                        last_seen_ts,
                        meta_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(station_key, source_url_norm) DO UPDATE SET
                        station_name=excluded.station_name,
                        station_name_norm=excluded.station_name_norm,
                        source_url=excluded.source_url,
                        source_kind=excluded.source_kind,
                        verified_by=excluded.verified_by,
                        confidence=excluded.confidence,
                        verified_at_utc=excluded.verified_at_utc,
                        last_seen_ts=excluded.last_seen_ts,
                        meta_json=excluded.meta_json
                    """,
                    (
                        station_key,
                        station_name,
                        station_name_norm,
                        source_url,
                        source_url_norm,
                        QF_VERIFIED_SOURCE_KIND,
                        QF_ADDON_ID,
                        confidence,
                        verified_at_utc,
                        last_seen_ts,
                        meta_json,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as err:
            self.logger.warning(
                "verified_source_upsert_failed",
                message=str(err),
                station_key=station_key,
                source_url=source_url_norm,
            )
            return False

        self.logger.debug(
            "verified_source_upsert_ok",
            station_key=station_key,
            source_url=source_url_norm,
            confidence=confidence,
        )
        return True

    def _resolve_song(self, station_input, station_id=""):
        lookup = self.StationLookupService(lambda msg: self.logger.debug("core_trace", message=msg))
        resolver = self.StreamResolver(lambda msg: self.logger.debug("core_trace", message=msg))
        fetcher = self.SongMetadataFetcher(lambda msg: self.logger.debug("core_trace", message=msg))
        discovery = self.NowPlayingDiscoveryService(lambda msg: self.logger.debug("core_trace", message=msg))

        station = None
        stream_seed = station_input
        station_id_norm = self._normalize_station_id(station_id)
        cache_key = self._build_resolution_cache_key(station_input, station_id=station_id)
        cached = self._get_cached_resolution(cache_key)
        if cached:
            station = cached.get("station")
            resolved = cached.get("resolved")
            origin_domains = set(cached.get("origin_domains") or [])
            self.logger.debug(
                "resolution_cache_hit",
                cache_key=cache_key,
                station=station.name if station else station_input,
            )
        else:
            if station_id_norm:
                try:
                    station = lookup.find_by_id(station_id_norm)
                    stream_seed = station.stream_url
                    self.logger.debug("station_id_match_ok", id=station_id_norm, name=station.name)
                except Exception as err:
                    self.logger.warning("station_id_lookup_failed", id=station_id_norm, error=str(err))
                    if not self.is_probable_url(station_input) and station_input:
                        station = self._find_station_by_name_with_fallback(
                            lookup,
                            station_input,
                            station_id_norm=station_id_norm,
                        )
                        stream_seed = station.stream_url
            elif not self.is_probable_url(station_input) and station_input:
                station = self._find_station_by_name_with_fallback(lookup, station_input)
                stream_seed = station.stream_url

            resolved = resolver.resolve(stream_seed, original_input=station_input)
            if station:
                resolved.station_name = station.name

            origin_domains = self._collect_origin_domains(station, resolved)
            self._store_cached_resolution(cache_key, station, resolved, origin_domains)

        def classify_source(url):
            if not url:
                return False, ""
            if not self.ORIGIN_ONLY_MODE:
                return True, "unrestricted"
            if self.is_origin_url(url, origin_domains):
                return True, "origin"
            if (
                self.ALLOW_OFFICIAL_CHAIN_SOURCES
                and discovery.is_trusted_candidate(url)
                and not self.is_non_origin_directory_url(url)
            ):
                return True, "official_player_chain"
            return False, "blocked_non_allowed"

        meta = {
            "station": station.name if station else station_input,
            "resolved_url": resolved.resolved_url,
            "delivery_url": resolved.delivery_url or "",
        }

        stream_song = None
        stream_error = ""
        try:
            stream_song = fetcher.fetch(resolved.resolved_url)
        except Exception as err:
            stream_error = str(err)

        station_name = station.name if station else station_input
        station_slug = ""
        if station:
            station_slug = (station.stationuuid or "").strip()
            if not station_slug:
                station_slug = self._normalize_station_name(station.name).replace(" ", "-")
        station_id_value = (station_id or "").strip()
        invalid_values = ["Unknown", "Radio Stream", "Internet Radio", "", station_name]
        station_hint_values = [station_name, station_slug, station_id_value]

        feed_pair_state = "no_candidate"
        stream_pair_state = "no_candidate"

        def validate_qf_pair(artist, title):
            a, t, state = self.prefilter_pair(
                artist,
                title,
                source="asm-qf",
                station_name=station_name,
                invalid_values=invalid_values,
                station_hint_values=station_hint_values,
            )
            return a, t, state

        stream_headers = stream_song.source_headers if stream_song else {}
        feed_candidates = discovery.discover_candidate_urls(
            resolved=resolved,
            station=station,
            stream_headers=stream_headers,
        )
        probe_candidates = discovery.filter_official_html_candidates(feed_candidates, station) or feed_candidates
        feed_song = None
        feed_retry_attempts = 3
        feed_retry_delay_seconds = 0.35
        for attempt in range(1, feed_retry_attempts + 1):
            feed_song = discovery.fetch_now_playing(probe_candidates, station_name=station_name)
            if not feed_song:
                feed_pair_state = "missing_field"
            else:
                _, _, feed_pair_state = validate_qf_pair(feed_song.artist, feed_song.title)
                if feed_pair_state == "ok":
                    break
            if attempt < feed_retry_attempts:
                time.sleep(feed_retry_delay_seconds)

        if feed_song and feed_pair_state == "ok":
            allowed, approval = classify_source(feed_song.source_url)
            if allowed:
                verified_source_url = (feed_song.source_url or "").strip() or resolved.resolved_url
                self._record_verified_source(
                    station_name=station_name,
                    source_url=verified_source_url,
                    confidence=0.95,
                    station_id=station_id,
                    meta={
                        "station_input": station_input,
                        "station_id": station_id,
                        "source_approval": approval,
                        "source_kind_raw": feed_song.source_kind,
                        "resolved_url": resolved.resolved_url,
                        "delivery_url": resolved.delivery_url or "",
                    },
                )
                return {
                    "status": "hit",
                    "artist": feed_song.artist,
                    "title": feed_song.title,
                    "source": feed_song.source_kind,
                    "reason": "station_name_match",
                    "meta": {**meta, "source_approval": approval, "source_url": feed_song.source_url},
                }
            self.logger.debug(
                "song_source_blocked",
                station=station_name,
                source_url=feed_song.source_url,
                source_kind=feed_song.source_kind,
            )
            return {
                "status": "blocked",
                "artist": "",
                "title": "",
                "source": feed_song.source_kind,
                "reason": "blocked_non_allowed_source",
                "meta": {**meta, "source_url": feed_song.source_url},
            }

        if feed_song and feed_pair_state != "ok":
            self.logger.debug(
                "song_pair_rejected",
                source_kind=feed_song.source_kind,
                source_url=feed_song.source_url,
                state=feed_pair_state,
                artist=feed_song.artist,
                title=feed_song.title,
            )

        if stream_song:
            _, _, stream_pair_state = validate_qf_pair(stream_song.artist, stream_song.title)
        if stream_song and stream_pair_state == "ok":
            allowed, approval = classify_source(stream_song.source_url)
            if allowed:
                verified_source_url = (stream_song.source_url or "").strip() or resolved.resolved_url
                self._record_verified_source(
                    station_name=station_name,
                    source_url=verified_source_url,
                    confidence=0.95,
                    station_id=station_id,
                    meta={
                        "station_input": station_input,
                        "station_id": station_id,
                        "source_approval": approval,
                        "source_kind_raw": stream_song.source_kind,
                        "resolved_url": resolved.resolved_url,
                        "delivery_url": resolved.delivery_url or "",
                    },
                )
                return {
                    "status": "hit",
                    "artist": stream_song.artist,
                    "title": stream_song.title,
                    "source": stream_song.source_kind,
                    "reason": "station_name_match",
                    "meta": {**meta, "source_approval": approval, "source_url": stream_song.source_url},
                }
            self.logger.debug(
                "song_source_blocked",
                station=station_name,
                source_url=stream_song.source_url,
                source_kind=stream_song.source_kind,
            )
            return {
                "status": "blocked",
                "artist": "",
                "title": "",
                "source": stream_song.source_kind,
                "reason": "blocked_non_allowed_source",
                "meta": {**meta, "source_url": stream_song.source_url},
            }

        if stream_song and stream_pair_state != "ok":
            self.logger.debug(
                "song_pair_rejected",
                source_kind=stream_song.source_kind,
                source_url=stream_song.source_url,
                state=stream_pair_state,
                artist=stream_song.artist,
                title=stream_song.title,
            )

        reason = "generic_or_non_song"
        if stream_error:
            reason = "no_stream_title"
            meta["stream_error"] = stream_error
        meta["feed_pair_state"] = feed_pair_state
        meta["stream_pair_state"] = stream_pair_state
        return {
            "status": "no_hit",
            "artist": "",
            "title": "",
            "source": "",
            "reason": reason,
            "meta": meta,
        }

    def _handle_request(self, req_id, station, station_id, mode, req_ts):
        if not req_id:
            return

        if not self._get_setting_bool("provider_finder_enabled", default=False):
            self.logger.info(
                "request_blocked",
                req_id=req_id,
                station=station,
                station_id=station_id,
                mode=mode,
                reason="qf_disabled",
            )
            self._write_response(
                req_id=req_id,
                status="blocked",
                reason="qf_disabled",
                meta={"mode": mode or "", "request_ts": req_ts or ""},
            )
            return

        if not self._ensure_imports():
            self.logger.error(
                "request_error",
                req_id=req_id,
                station=station,
                station_id=station_id,
                mode=mode,
                reason="import_failed",
                error=self._import_error,
            )
            self._write_response(
                req_id=req_id,
                status="error",
                reason="import_failed",
                meta={"error": self._import_error},
            )
            return

        station_name = (station or "").strip()
        if not station_name and not (station_id or "").strip():
            self.logger.info(
                "request_nohit",
                req_id=req_id,
                station_id=station_id,
                mode=mode,
                reason="missing_station",
            )
            self._write_response(
                req_id=req_id,
                status="no_hit",
                reason="missing_station",
                meta={"mode": mode or "", "request_ts": req_ts or ""},
            )
            return

        try:
            result = self._resolve_song(station_name, station_id=station_id)
            self.logger.info(
                "request_result",
                req_id=req_id,
                station=station,
                station_id=station_id,
                mode=mode,
                status=result.get("status") or "error",
                reason=result.get("reason") or "",
            )
            self._write_response(
                req_id=req_id,
                status=result.get("status") or "error",
                artist=result.get("artist") or "",
                title=result.get("title") or "",
                source=result.get("source") or "",
                reason=result.get("reason") or "",
                meta=result.get("meta") or {},
            )
        except Exception as err:
            message = str(err)
            status = "timeout" if "timeout" in message.lower() else "error"
            self.logger.error(
                "request_exception",
                req_id=req_id,
                station=station,
                station_id=station_id,
                mode=mode,
                status=status,
                error=message,
            )
            self._write_response(
                req_id=req_id,
                status=status,
                reason="resolver_exception",
                meta={"error": message},
            )

    def run(self):
        self.logger.info("service_started")
        while not self.abortRequested():
            req_id = (WINDOW.getProperty(REQ_ID) or "").strip()
            if req_id and req_id != self.last_request_id:
                self.last_request_id = req_id
                station = WINDOW.getProperty(REQ_STATION) or ""
                station_id = WINDOW.getProperty(REQ_STATION_ID) or ""
                mode = WINDOW.getProperty(REQ_MODE) or ""
                req_ts = WINDOW.getProperty(REQ_TS) or ""
                self.logger.info(
                    "request_received",
                    req_id=req_id,
                    station=station,
                    station_id=station_id,
                    station_key_hint=self._build_station_key(station, station_id=station_id),
                    mode=mode,
                    request_ts=req_ts,
                )
                self._handle_request(req_id, station, station_id, mode, req_ts)

            if self.waitForAbort(0.25):
                break

        self.logger.info("service_stopped")


if __name__ == "__main__":
    QFBridgeService().run()
