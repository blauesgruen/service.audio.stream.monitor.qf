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
RES_FOR_REQ_ID = "RadioMonitor.QF.Response.ForReqId"

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
        self._qf_station_state: dict[str, dict] = {}
        self.QF_SERVICE_GUI_PARITY_ENABLED = False
        self.QF_HOLD_SECONDS = 0.0
        self.QF_NO_HIT_CONFIRM = 1
        self.QF_EMPTY_CONFIRM = 1
        self.QF_FEED_RETRY_ATTEMPTS = 3
        self.QF_FEED_RETRY_DELAY_SECONDS = 0.35
        self.QF_STATE_MAX_STATIONS = 64
        self.QF_REQUEST_GAP_BUFFER_SECONDS = 2.0
        self.QF_REQUEST_GAP_MAX_SECONDS = 90.0
        self.QF_REQUEST_GAP_USE_CLIENT_TS = True
        self.QF_REQUEST_GAP_EMA_ALPHA = 0.4
        self.QF_PENDING_FEED_CONFIRM_WITHOUT_HISTORY = False
        self.QF_TELEMETRY_ENABLED = True
        self.QF_FASTPATH_VERIFIED_SOURCE_ENABLED = True
        self.QF_VERIFIED_SOURCE_MAX_AGE_SECONDS = 43200
        self.QF_PHASE_TIMING_PRECISION = 3
        self.QF_RESULT_CACHE_ENABLED = True
        self.QF_RESULT_CACHE_TTL_SECONDS = 12
        self.QF_DISCOVERY_QUICKPASS_ENABLED = True
        self.QF_DISCOVERY_QUICKPASS_MAX_CANDIDATES = 3
        self.QF_DISCOVERY_QUICKPASS_MAX_SECONDS = 1.2
        self.QF_FEED_RETRY_MIN_ATTEMPTS = 1
        self.QF_FEED_RETRY_MAX_ATTEMPTS = 3
        self.QF_FEED_RETRY_SHORT_GAP_SECONDS = 8.0
        self.QF_FEED_RETRY_LONG_GAP_SECONDS = 25.0
        self._verified_source_repo = None
        self._result_cache: dict[str, dict] = {}
        self._lookup_service = None
        self._resolver_service = None
        self._fetcher_service = None
        self._discovery_service = None

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
            from app.config import (
                ALLOW_OFFICIAL_CHAIN_SOURCES,
                ORIGIN_ONLY_MODE,
                QF_EMPTY_CONFIRM,
                QF_DISCOVERY_QUICKPASS_ENABLED,
                QF_DISCOVERY_QUICKPASS_MAX_CANDIDATES,
                QF_DISCOVERY_QUICKPASS_MAX_SECONDS,
                QF_FEED_RETRY_ATTEMPTS,
                QF_FEED_RETRY_DELAY_SECONDS,
                QF_FEED_RETRY_MIN_ATTEMPTS,
                QF_FEED_RETRY_MAX_ATTEMPTS,
                QF_FEED_RETRY_SHORT_GAP_SECONDS,
                QF_FEED_RETRY_LONG_GAP_SECONDS,
                QF_HOLD_SECONDS,
                QF_NO_HIT_CONFIRM,
                QF_PENDING_FEED_CONFIRM_WITHOUT_HISTORY,
                QF_PHASE_TIMING_PRECISION,
                QF_RESULT_CACHE_ENABLED,
                QF_RESULT_CACHE_TTL_SECONDS,
                QF_REQUEST_GAP_BUFFER_SECONDS,
                QF_REQUEST_GAP_EMA_ALPHA,
                QF_REQUEST_GAP_MAX_SECONDS,
                QF_REQUEST_GAP_USE_CLIENT_TS,
                QF_SERVICE_GUI_PARITY_ENABLED,
                QF_STATE_MAX_STATIONS,
                QF_TELEMETRY_ENABLED,
                QF_FASTPATH_VERIFIED_SOURCE_ENABLED,
                QF_VERIFIED_SOURCE_MAX_AGE_SECONDS,
            )
            from app.metadata import SongMetadataFetcher
            from app.now_playing_discovery import NowPlayingDiscoveryService
            from app.song_validation import prefilter_pair
            from app.station_lookup import StationLookupService
            from app.stream_resolver import StreamResolver
            from app.source_registry import VerifiedSourceRepository
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
        self.QF_SERVICE_GUI_PARITY_ENABLED = bool(QF_SERVICE_GUI_PARITY_ENABLED)
        self.QF_HOLD_SECONDS = max(0.0, float(QF_HOLD_SECONDS))
        self.QF_NO_HIT_CONFIRM = max(1, int(QF_NO_HIT_CONFIRM))
        self.QF_EMPTY_CONFIRM = max(1, int(QF_EMPTY_CONFIRM))
        self.QF_FEED_RETRY_ATTEMPTS = max(1, int(QF_FEED_RETRY_ATTEMPTS))
        self.QF_FEED_RETRY_DELAY_SECONDS = max(0.0, float(QF_FEED_RETRY_DELAY_SECONDS))
        self.QF_STATE_MAX_STATIONS = max(16, int(QF_STATE_MAX_STATIONS))
        self.QF_REQUEST_GAP_BUFFER_SECONDS = max(0.0, float(QF_REQUEST_GAP_BUFFER_SECONDS))
        self.QF_REQUEST_GAP_MAX_SECONDS = max(5.0, float(QF_REQUEST_GAP_MAX_SECONDS))
        self.QF_REQUEST_GAP_USE_CLIENT_TS = bool(QF_REQUEST_GAP_USE_CLIENT_TS)
        self.QF_REQUEST_GAP_EMA_ALPHA = min(1.0, max(0.05, float(QF_REQUEST_GAP_EMA_ALPHA)))
        self.QF_PENDING_FEED_CONFIRM_WITHOUT_HISTORY = bool(QF_PENDING_FEED_CONFIRM_WITHOUT_HISTORY)
        self.QF_TELEMETRY_ENABLED = bool(QF_TELEMETRY_ENABLED)
        self.QF_FASTPATH_VERIFIED_SOURCE_ENABLED = bool(QF_FASTPATH_VERIFIED_SOURCE_ENABLED)
        self.QF_VERIFIED_SOURCE_MAX_AGE_SECONDS = max(0, int(QF_VERIFIED_SOURCE_MAX_AGE_SECONDS))
        self.QF_PHASE_TIMING_PRECISION = max(1, int(QF_PHASE_TIMING_PRECISION))
        self.QF_RESULT_CACHE_ENABLED = bool(QF_RESULT_CACHE_ENABLED)
        self.QF_RESULT_CACHE_TTL_SECONDS = max(0, int(QF_RESULT_CACHE_TTL_SECONDS))
        self.QF_DISCOVERY_QUICKPASS_ENABLED = bool(QF_DISCOVERY_QUICKPASS_ENABLED)
        self.QF_DISCOVERY_QUICKPASS_MAX_CANDIDATES = max(1, int(QF_DISCOVERY_QUICKPASS_MAX_CANDIDATES))
        self.QF_DISCOVERY_QUICKPASS_MAX_SECONDS = max(0.1, float(QF_DISCOVERY_QUICKPASS_MAX_SECONDS))
        self.QF_FEED_RETRY_MIN_ATTEMPTS = max(1, int(QF_FEED_RETRY_MIN_ATTEMPTS))
        self.QF_FEED_RETRY_MAX_ATTEMPTS = max(self.QF_FEED_RETRY_MIN_ATTEMPTS, int(QF_FEED_RETRY_MAX_ATTEMPTS))
        self.QF_FEED_RETRY_SHORT_GAP_SECONDS = max(0.0, float(QF_FEED_RETRY_SHORT_GAP_SECONDS))
        self.QF_FEED_RETRY_LONG_GAP_SECONDS = max(
            self.QF_FEED_RETRY_SHORT_GAP_SECONDS,
            float(QF_FEED_RETRY_LONG_GAP_SECONDS),
        )
        self.SongMetadataFetcher = SongMetadataFetcher
        self.NowPlayingDiscoveryService = NowPlayingDiscoveryService
        self.prefilter_pair = prefilter_pair
        self.StationLookupService = StationLookupService
        self.StreamResolver = StreamResolver
        self.VerifiedSourceRepository = VerifiedSourceRepository
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
        self._set_property(RES_FOR_REQ_ID, "")

    def _write_response(
        self,
        req_id,
        status,
        artist="",
        title="",
        source="",
        reason="",
        meta=None,
        response_for_req_id="",
        decision_latency_s=None,
    ):
        response_for_req_id = str(response_for_req_id or req_id or "").strip()
        response_ts = int(time.time())
        self._clear_response()
        self._set_property(RES_STATUS, status)
        self._set_property(RES_ARTIST, artist)
        self._set_property(RES_TITLE, title)
        self._set_property(RES_SOURCE, source)
        self._set_property(RES_REASON, reason)
        if meta:
            self._set_property(RES_META, json.dumps(meta, ensure_ascii=False))
        self._set_property(RES_TS, str(response_ts))
        self._set_property(RES_FOR_REQ_ID, response_for_req_id)
        # Write response id last so clients can treat it as commit marker.
        self._set_property(RES_ID, req_id)
        self.logger.info(
            "response_written",
            req_id=req_id,
            response_for_req_id=response_for_req_id,
            response_ts=response_ts,
            status=status,
            reason=reason,
            decision_latency_s=decision_latency_s,
        )

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

    def _parse_request_ts(self, raw_value):
        text = str(raw_value or "").strip()
        if not text:
            return 0.0
        try:
            value = float(text)
        except Exception:
            return 0.0
        if value <= 0.0:
            return 0.0
        # Accept both epoch seconds and epoch milliseconds.
        if value > 10_000_000_000:
            value /= 1000.0
        return value

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

    def _get_verified_source_repository(self):
        if self._verified_source_repo is not None:
            return self._verified_source_repo

        db_path = self._get_qf_db_path()
        if not db_path:
            return None

        try:
            self._verified_source_repo = self.VerifiedSourceRepository(
                db_path=db_path,
                normalize_url=self._normalize_url,
                log=lambda event, detail="": self.logger.debug(event, detail=detail),
            )
        except Exception as err:
            self.logger.warning("verified_source_repo_init_failed", error=str(err))
            self._verified_source_repo = None
        return self._verified_source_repo

    def _get_core_services(self):
        if self._lookup_service is None:
            self._lookup_service = self.StationLookupService(lambda msg: self.logger.debug("core_trace", message=msg))
        if self._resolver_service is None:
            self._resolver_service = self.StreamResolver(lambda msg: self.logger.debug("core_trace", message=msg))
        if self._fetcher_service is None:
            self._fetcher_service = self.SongMetadataFetcher(lambda msg: self.logger.debug("core_trace", message=msg))
        if self._discovery_service is None:
            self._discovery_service = self.NowPlayingDiscoveryService(lambda msg: self.logger.debug("core_trace", message=msg))
        return (
            self._lookup_service,
            self._resolver_service,
            self._fetcher_service,
            self._discovery_service,
        )

    def _get_cached_result(self, station_key):
        if not self.QF_RESULT_CACHE_ENABLED:
            return None
        key = str(station_key or "").strip()
        if not key:
            return None
        item = self._result_cache.get(key)
        if not item:
            return None
        age = time.time() - float(item.get("ts") or 0.0)
        if age > float(self.QF_RESULT_CACHE_TTL_SECONDS):
            self._result_cache.pop(key, None)
            return None
        return item.get("result")

    def _store_cached_result(self, station_key, result):
        if not self.QF_RESULT_CACHE_ENABLED:
            return
        key = str(station_key or "").strip()
        if not key:
            return
        if not isinstance(result, dict):
            return
        if str(result.get("status") or "") != "hit":
            return
        self._result_cache[key] = {
            "ts": time.time(),
            "result": dict(result),
        }
        if len(self._result_cache) > 128:
            oldest_key = min(
                self._result_cache,
                key=lambda item_key: float(self._result_cache[item_key].get("ts") or 0.0),
            )
            self._result_cache.pop(oldest_key, None)

    def _invalidate_cached_result(self, station_key):
        key = str(station_key or "").strip()
        if not key:
            return
        self._result_cache.pop(key, None)

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

    def _get_station_state(self, station_key):
        key = str(station_key or "").strip()
        if not key:
            key = "global"
        state = self._qf_station_state.get(key)
        if state:
            return state
        state = {
            "station_key": key,
            "last_hit_ts": 0.0,
            "last_strong_hit_ts": 0.0,
            "last_request_ts": 0.0,
            "last_client_request_ts": 0.0,
            "request_gap_ema": 0.0,
            "last_artist": "",
            "last_title": "",
            "last_source": "",
            "last_reason": "",
            "last_no_hit_reason": "",
            "last_meta": {},
            "pending_hit_key": "",
            "pending_hit_count": 0,
            "pending_hit_ts": 0.0,
            "no_hit_streak": 0,
            "empty_streak": 0,
            "updated_ts": 0.0,
        }
        self._qf_station_state[key] = state
        return state

    def _prune_station_state(self):
        max_items = max(16, int(self.QF_STATE_MAX_STATIONS or 64))
        if len(self._qf_station_state) <= max_items:
            return
        oldest_key = min(
            self._qf_station_state,
            key=lambda item_key: float(self._qf_station_state[item_key].get("updated_ts") or 0.0),
        )
        self._qf_station_state.pop(oldest_key, None)

    def _trace_qf_decision(self, station_key, action, result, state, **extra):
        meta = result.get("meta") or {}
        effective_hold_seconds = extra.pop("effective_hold_seconds", self.QF_HOLD_SECONDS)
        self.logger.debug(
            "qf_decision_trace",
            station_key=station_key,
            action=action,
            status=result.get("status") or "",
            reason=result.get("reason") or "",
            source=result.get("source") or "",
            artist=result.get("artist") or "",
            title=result.get("title") or "",
            feed_pair_state=meta.get("feed_pair_state") or "",
            stream_pair_state=meta.get("stream_pair_state") or "",
            hold_seconds=round(float(effective_hold_seconds), 3),
            no_hit_streak=state.get("no_hit_streak") or 0,
            empty_streak=state.get("empty_streak") or 0,
            last_hit_age=extra.pop("last_hit_age", ""),
            hold_remaining=extra.pop("hold_remaining", ""),
            request_gap=extra.pop("request_gap", ""),
            **extra,
        )

    def _apply_qf_parity_policy(self, station_key, result, request_ts=0.0):
        state = self._get_station_state(station_key)
        now = time.time()
        last_request_ts = float(state.get("last_request_ts") or 0.0)
        request_gap_server = (now - last_request_ts) if last_request_ts > 0 else 0.0
        state["last_request_ts"] = now
        request_gap_client = 0.0
        gap_source = "none"
        if self.QF_REQUEST_GAP_USE_CLIENT_TS:
            req_ts = float(request_ts or 0.0)
            last_client_request_ts = float(state.get("last_client_request_ts") or 0.0)
            if req_ts > 0.0 and last_client_request_ts > 0.0 and req_ts > last_client_request_ts:
                request_gap_client = req_ts - last_client_request_ts
                gap_source = "client"
            if req_ts > 0.0:
                state["last_client_request_ts"] = req_ts

        request_gap_raw = 0.0
        if request_gap_client > 0.0:
            request_gap_raw = request_gap_client
        elif request_gap_server > 0.0:
            request_gap_raw = request_gap_server
            gap_source = "server"

        request_gap_smoothed = float(state.get("request_gap_ema") or 0.0)
        if request_gap_raw > 0.0:
            alpha = float(self.QF_REQUEST_GAP_EMA_ALPHA)
            if request_gap_smoothed <= 0.0:
                request_gap_smoothed = request_gap_raw
            else:
                request_gap_smoothed = (alpha * request_gap_raw) + ((1.0 - alpha) * request_gap_smoothed)
            state["request_gap_ema"] = request_gap_smoothed

        state["updated_ts"] = now
        status = str(result.get("status") or "")
        reason = str(result.get("reason") or "")
        artist = str(result.get("artist") or "")
        title = str(result.get("title") or "")
        source = str(result.get("source") or "")
        meta = result.get("meta") or {}
        effective_hold_seconds = float(self.QF_HOLD_SECONDS)

        def _clear_pending_hit():
            state["pending_hit_key"] = ""
            state["pending_hit_count"] = 0
            state["pending_hit_ts"] = 0.0

        pending_bypassed = False
        if status == "hit" and artist and title:
            has_last_pair_before = bool(state.get("last_artist") and state.get("last_title"))
            stream_pair_state = str(meta.get("stream_pair_state") or "")
            is_feed_hit = str(source).startswith("web_feed_")
            weak_stream_signal = stream_pair_state in {"", "no_candidate", "missing_field"}
            need_pending_confirmation = bool(self.QF_PENDING_FEED_CONFIRM_WITHOUT_HISTORY)
            if (
                self.QF_SERVICE_GUI_PARITY_ENABLED
                and is_feed_hit
                and weak_stream_signal
                and not has_last_pair_before
                and need_pending_confirmation
            ):
                pending_key = f"{artist.lower()}|{title.lower()}|{source}"
                previous_key = str(state.get("pending_hit_key") or "")
                previous_count = int(state.get("pending_hit_count") or 0)
                if pending_key == previous_key and previous_count > 0:
                    state["pending_hit_count"] = previous_count + 1
                else:
                    state["pending_hit_key"] = pending_key
                    state["pending_hit_count"] = 1
                state["pending_hit_ts"] = now

                if int(state.get("pending_hit_count") or 0) < 2:
                    pending_result = {
                        "status": "no_hit",
                        "artist": "",
                        "title": "",
                        "source": "",
                        "reason": "pending_feed_confirmation",
                        "meta": {
                            **meta,
                            "pending_hit": True,
                            "pending_hit_count": state.get("pending_hit_count") or 0,
                            "pending_pair": f"{artist} - {title}",
                        },
                    }
                    self._trace_qf_decision(
                        station_key,
                        "pending_hit",
                        pending_result,
                        state,
                        last_hit_age="",
                        hold_remaining=0.0,
                        request_gap=round(request_gap_raw, 3),
                        request_gap_smoothed=round(request_gap_smoothed, 3),
                        gap_source=gap_source,
                        effective_hold_seconds=effective_hold_seconds,
                    )
                    self._prune_station_state()
                    return pending_result

                _clear_pending_hit()
            else:
                pending_bypassed = bool(
                    self.QF_SERVICE_GUI_PARITY_ENABLED
                    and is_feed_hit
                    and weak_stream_signal
                    and not has_last_pair_before
                    and not need_pending_confirmation
                )
                if pending_bypassed:
                    meta = {**meta, "pending_bypassed": True}
                    result = {
                        "status": status,
                        "artist": artist,
                        "title": title,
                        "source": source,
                        "reason": reason,
                        "meta": meta,
                    }
                _clear_pending_hit()

            # GUI-parity guard: if we only keep seeing the same feed pair without stream support,
            # do not let that stale feed keep the song alive forever.
            if status == "hit" and is_feed_hit and weak_stream_signal and has_last_pair_before:
                same_pair = (
                    artist.strip().lower() == str(state.get("last_artist") or "").strip().lower()
                    and title.strip().lower() == str(state.get("last_title") or "").strip().lower()
                )
                reference_ts = float(state.get("last_strong_hit_ts") or 0.0)
                if reference_ts <= 0.0:
                    reference_ts = float(state.get("last_hit_ts") or 0.0)
                weak_age = (now - reference_ts) if reference_ts > 0.0 else 0.0
                if same_pair and reference_ts > 0.0 and weak_age > effective_hold_seconds:
                    status = "no_hit"
                    reason = "generic_or_non_song"
                    artist = ""
                    title = ""
                    source = ""
                    meta = {
                        **meta,
                        "stale_feed_only": True,
                        "stale_feed_age": round(weak_age, 3),
                        "stale_feed_hold": round(effective_hold_seconds, 3),
                    }
                    result = {
                        "status": status,
                        "artist": artist,
                        "title": title,
                        "source": source,
                        "reason": reason,
                        "meta": meta,
                    }

        if status == "hit" and artist and title:
            stream_pair_state = str(meta.get("stream_pair_state") or "")
            is_feed_hit = str(source).startswith("web_feed_")
            weak_stream_signal = stream_pair_state in {"", "no_candidate", "missing_field"}

            state["last_hit_ts"] = now
            if not (is_feed_hit and weak_stream_signal):
                state["last_strong_hit_ts"] = now
            state["last_artist"] = artist
            state["last_title"] = title
            state["last_source"] = source
            state["last_reason"] = reason
            state["last_no_hit_reason"] = ""
            state["last_meta"] = dict(meta)
            state["no_hit_streak"] = 0
            state["empty_streak"] = 0
            self._trace_qf_decision(
                station_key,
                "accept_hit",
                result,
                state,
                last_hit_age=0.0,
                hold_remaining=round(effective_hold_seconds, 3),
                request_gap=round(request_gap_raw, 3),
                request_gap_smoothed=round(request_gap_smoothed, 3),
                gap_source=gap_source,
                effective_hold_seconds=effective_hold_seconds,
                pending_bypassed=pending_bypassed,
            )
            self._prune_station_state()
            return result

        if status != "no_hit" or not self.QF_SERVICE_GUI_PARITY_ENABLED:
            if status == "no_hit":
                state["last_no_hit_reason"] = reason
                state["no_hit_streak"] = int(state.get("no_hit_streak") or 0) + 1
            self._trace_qf_decision(
                station_key,
                "passthrough",
                result,
                state,
                request_gap=round(request_gap_raw, 3),
                request_gap_smoothed=round(request_gap_smoothed, 3),
                gap_source=gap_source,
                effective_hold_seconds=effective_hold_seconds,
            )
            self._prune_station_state()
            return result

        state["last_no_hit_reason"] = reason
        state["no_hit_streak"] = int(state.get("no_hit_streak") or 0) + 1

        feed_pair_state = str(meta.get("feed_pair_state") or "")
        stream_pair_state = str(meta.get("stream_pair_state") or "")
        empty_signals = {"missing_field", "no_candidate"}
        is_empty_signal = reason == "generic_or_non_song" and (
            feed_pair_state in empty_signals or stream_pair_state in empty_signals
        )
        if is_empty_signal:
            state["empty_streak"] = int(state.get("empty_streak") or 0) + 1
        else:
            state["empty_streak"] = 0

        last_hit_ts = float(state.get("last_hit_ts") or 0.0)
        has_last_pair = bool(state.get("last_artist") and state.get("last_title"))
        last_hit_age = (now - last_hit_ts) if last_hit_ts > 0 else float("inf")
        hold_remaining = max(0.0, effective_hold_seconds - last_hit_age) if has_last_pair else 0.0
        hold_active = has_last_pair and hold_remaining > 0

        no_hit_confirmed = int(state.get("no_hit_streak") or 0) >= int(self.QF_NO_HIT_CONFIRM)
        empty_confirmed = int(state.get("empty_streak") or 0) >= int(self.QF_EMPTY_CONFIRM)

        # Song-Ende priorisieren: bestätigte no-hit/empty-Signale dürfen Hold sofort beenden.
        if no_hit_confirmed or empty_confirmed:
            state["last_artist"] = ""
            state["last_title"] = ""
            state["last_source"] = ""
            state["last_reason"] = ""
            state["last_meta"] = {}
            state["last_hit_ts"] = 0.0
            state["last_strong_hit_ts"] = 0.0
            state["no_hit_streak"] = 0
            state["empty_streak"] = 0
            _clear_pending_hit()
            self._trace_qf_decision(
                station_key,
                "confirm_no_hit",
                result,
                state,
                last_hit_age=round(last_hit_age, 3) if last_hit_age != float("inf") else "",
                hold_remaining=round(hold_remaining, 3),
                request_gap=round(request_gap_raw, 3),
                request_gap_smoothed=round(request_gap_smoothed, 3),
                gap_source=gap_source,
                effective_hold_seconds=effective_hold_seconds,
                no_hit_confirmed=no_hit_confirmed,
                empty_confirmed=empty_confirmed,
            )
            self._prune_station_state()
            return result

        if hold_active:
            hold_result = {
                "status": "hit",
                "artist": state.get("last_artist") or "",
                "title": state.get("last_title") or "",
                "source": state.get("last_source") or "asm-qf_hold",
                "reason": "hold_last_song",
                "meta": {
                    **(state.get("last_meta") or {}),
                    "hold": True,
                    "hold_remaining": round(hold_remaining, 3),
                    "hold_seconds": round(effective_hold_seconds, 3),
                    "no_hit_reason": reason,
                    "no_hit_streak": state.get("no_hit_streak") or 0,
                    "empty_streak": state.get("empty_streak") or 0,
                    "feed_pair_state": feed_pair_state,
                    "stream_pair_state": stream_pair_state,
                },
            }
            self._trace_qf_decision(
                station_key,
                "hold_last_song",
                hold_result,
                state,
                last_hit_age=round(last_hit_age, 3),
                hold_remaining=round(hold_remaining, 3),
                request_gap=round(request_gap_raw, 3),
                request_gap_smoothed=round(request_gap_smoothed, 3),
                gap_source=gap_source,
                effective_hold_seconds=effective_hold_seconds,
            )
            self._prune_station_state()
            return hold_result


        self._trace_qf_decision(
            station_key,
            "soft_no_hit",
            result,
            state,
            last_hit_age=round(last_hit_age, 3) if last_hit_age != float("inf") else "",
            hold_remaining=0.0,
            request_gap=round(request_gap_raw, 3),
            request_gap_smoothed=round(request_gap_smoothed, 3),
            gap_source=gap_source,
            effective_hold_seconds=effective_hold_seconds,
        )
        self._prune_station_state()
        return result

    def _resolve_song(self, station_input, station_id="", station_key=""):
        lookup, resolver, fetcher, discovery = self._get_core_services()

        phase_timings = {}
        verified_fastpath_state = "disabled"

        def _mark_phase(name, started_at):
            if not self.QF_TELEMETRY_ENABLED:
                return
            phase_timings[name] = round(
                max(0.0, time.time() - float(started_at)),
                int(self.QF_PHASE_TIMING_PRECISION),
            )

        def _attach_phase_meta(meta_obj):
            if not self.QF_TELEMETRY_ENABLED:
                return meta_obj
            merged = dict(meta_obj or {})
            if phase_timings:
                merged["phase_timings_s"] = dict(phase_timings)
            if verified_fastpath_state:
                merged["verified_fastpath_state"] = verified_fastpath_state
            return merged

        station = None
        stream_seed = station_input
        station_id_norm = self._normalize_station_id(station_id)

        station_name_hint = (station_input or "").strip()
        station_id_value = (station_id or "").strip()

        fastpath_started = time.time()
        if self.QF_FASTPATH_VERIFIED_SOURCE_ENABLED:
            verified_fastpath_state = "miss"
            station_key = self._build_station_key(station_name_hint, station_id=station_id)
            source_repo = self._get_verified_source_repository()
            if source_repo and station_key:
                candidate = source_repo.get_preferred_source(
                    station_key,
                    max_age_seconds=int(self.QF_VERIFIED_SOURCE_MAX_AGE_SECONDS),
                )
                if candidate:
                    verified_source_url = str(candidate.get("source_url") or "").strip()
                    if verified_source_url:
                        verified_fastpath_state = "candidate"
                        probe_started = time.time()
                        fast_song = None
                        try:
                            fast_song = fetcher.fetch(verified_source_url)
                            verified_fastpath_state = "probe_ok"
                        except Exception as err:
                            verified_fastpath_state = "probe_error"
                            self.logger.debug(
                                "verified_source_fastpath_probe_failed",
                                station_key=station_key,
                                source_url=verified_source_url,
                                error=str(err),
                            )
                        _mark_phase("verified_source_probe", probe_started)

                        if fast_song:
                            a, t, pair_state = self.prefilter_pair(
                                fast_song.artist,
                                fast_song.title,
                                source="asm-qf",
                                station_name=station_name_hint,
                                invalid_values=["Unknown", "Radio Stream", "Internet Radio", "", station_name_hint],
                                station_hint_values=[station_name_hint, station_id_value, station_id_norm],
                            )
                            if pair_state == "ok":
                                verified_fastpath_state = "hit"
                                _mark_phase("verified_source_lookup", fastpath_started)
                                return {
                                    "status": "hit",
                                    "artist": a,
                                    "title": t,
                                    "source": fast_song.source_kind,
                                    "reason": "verified_source_fastpath",
                                    "meta": _attach_phase_meta(
                                        {
                                            "station": station_name_hint,
                                            "source_approval": "verified_source_cache",
                                            "source_url": fast_song.source_url or verified_source_url,
                                            "resolved_url": "",
                                            "delivery_url": "",
                                        }
                                    ),
                                }
                            verified_fastpath_state = f"rejected_{pair_state}"
        _mark_phase("verified_source_lookup", fastpath_started)

        resolution_started = time.time()
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
        _mark_phase("resolution", resolution_started)

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
        stream_probe_started = time.time()
        try:
            stream_song = fetcher.fetch(resolved.resolved_url)
        except Exception as err:
            stream_error = str(err)
        _mark_phase("stream_probe", stream_probe_started)

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
        if stream_song:
            _, _, stream_pair_state = validate_qf_pair(stream_song.artist, stream_song.title)
        discovery_started = time.time()
        feed_candidates = discovery.discover_candidate_urls(
            resolved=resolved,
            station=station,
            stream_headers=stream_headers,
        )
        probe_candidates = discovery.filter_official_html_candidates(feed_candidates, station) or feed_candidates
        feed_song = None
        feed_retry_attempts = int(self.QF_FEED_RETRY_ATTEMPTS)
        feed_retry_attempts = max(self.QF_FEED_RETRY_MIN_ATTEMPTS, feed_retry_attempts)
        feed_retry_attempts = min(self.QF_FEED_RETRY_MAX_ATTEMPTS, feed_retry_attempts)
        request_gap_smoothed = 0.0
        if station_key:
            state = self._get_station_state(station_key)
            request_gap_smoothed = float(state.get("request_gap_ema") or 0.0)
            if request_gap_smoothed > 0.0 and request_gap_smoothed <= float(self.QF_FEED_RETRY_SHORT_GAP_SECONDS):
                feed_retry_attempts = self.QF_FEED_RETRY_MIN_ATTEMPTS
            elif request_gap_smoothed >= float(self.QF_FEED_RETRY_LONG_GAP_SECONDS):
                feed_retry_attempts = self.QF_FEED_RETRY_MAX_ATTEMPTS

        feed_retry_delay_seconds = float(self.QF_FEED_RETRY_DELAY_SECONDS)
        if self.QF_DISCOVERY_QUICKPASS_ENABLED and probe_candidates:
            quick_pass_started = time.time()
            feed_song = discovery.fetch_now_playing(
                probe_candidates,
                station_name=station_name,
                max_candidates=self.QF_DISCOVERY_QUICKPASS_MAX_CANDIDATES,
                max_elapsed_seconds=self.QF_DISCOVERY_QUICKPASS_MAX_SECONDS,
            )
            _mark_phase("discovery_quick_pass", quick_pass_started)
            if not feed_song:
                feed_pair_state = "missing_field"
            else:
                _, _, feed_pair_state = validate_qf_pair(feed_song.artist, feed_song.title)

        if feed_pair_state != "ok":
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
        _mark_phase("discovery", discovery_started)

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
                    "meta": _attach_phase_meta(
                        {
                            **meta,
                            "source_approval": approval,
                            "source_url": feed_song.source_url,
                            "feed_pair_state": feed_pair_state,
                            "stream_pair_state": stream_pair_state,
                            "feed_age_minutes": feed_song.age_minutes,
                            "feed_retry_attempts_effective": feed_retry_attempts,
                            "request_gap_smoothed": round(request_gap_smoothed, int(self.QF_PHASE_TIMING_PRECISION)),
                        }
                    ),
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
                "meta": _attach_phase_meta({**meta, "source_url": feed_song.source_url}),
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
                    "meta": _attach_phase_meta(
                        {**meta, "source_approval": approval, "source_url": stream_song.source_url}
                    ),
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
                "meta": _attach_phase_meta({**meta, "source_url": stream_song.source_url}),
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
        meta["feed_retry_attempts_effective"] = feed_retry_attempts
        meta["request_gap_smoothed"] = round(request_gap_smoothed, int(self.QF_PHASE_TIMING_PRECISION))
        return {
            "status": "no_hit",
            "artist": "",
            "title": "",
            "source": "",
            "reason": reason,
            "meta": _attach_phase_meta(meta),
        }

    def _handle_request(self, req_id, station, station_id, mode, req_ts):
        if not req_id:
            return

        decision_start_ts = time.time()
        request_finalized = False

        def _decision_latency_seconds():
            return round(max(0.0, time.time() - decision_start_ts), 3)

        def _finalize_request(status, reason="", artist="", title="", source="", meta=None):
            nonlocal request_finalized
            if request_finalized:
                self.logger.warning(
                    "request_result_duplicate_finalize",
                    req_id=req_id,
                    station=station,
                    station_id=station_id,
                    mode=mode,
                    status=status,
                    reason=reason,
                )
                return

            request_finalized = True
            decision_latency_s = _decision_latency_seconds()
            final_meta = dict(meta or {})
            final_meta["decision_latency_s"] = decision_latency_s

            self.logger.info(
                "request_result",
                req_id=req_id,
                station=station,
                station_id=station_id,
                mode=mode,
                status=status,
                reason=reason,
                decision_latency_s=decision_latency_s,
            )
            self._write_response(
                req_id=req_id,
                status=status,
                artist=artist,
                title=title,
                source=source,
                reason=reason,
                meta=final_meta,
                response_for_req_id=req_id,
                decision_latency_s=decision_latency_s,
            )

        self.logger.debug(
            "request_result_pending",
            req_id=req_id,
            station=station,
            station_id=station_id,
            mode=mode,
            status="pending",
        )

        if not self._get_setting_bool("provider_finder_enabled", default=False):
            self.logger.info(
                "request_blocked",
                req_id=req_id,
                station=station,
                station_id=station_id,
                mode=mode,
                reason="qf_disabled",
            )
            _finalize_request(
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
            _finalize_request(
                status="error",
                reason="import_failed",
                meta={"error": self._import_error},
            )
            return

        station_name = (station or "").strip()
        station_key = self._build_station_key(station_name, station_id=station_id)
        if not station_name and not (station_id or "").strip():
            self.logger.info(
                "request_nohit",
                req_id=req_id,
                station_id=station_id,
                mode=mode,
                reason="missing_station",
            )
            _finalize_request(
                status="no_hit",
                reason="missing_station",
                meta={"mode": mode or "", "request_ts": req_ts or ""},
            )
            return

        try:
            cached_result = self._get_cached_result(station_key)
            if cached_result:
                cached_meta = dict((cached_result.get("meta") or {}))
                cached_meta["result_cache_hit"] = True
                result = {
                    "status": cached_result.get("status") or "hit",
                    "artist": cached_result.get("artist") or "",
                    "title": cached_result.get("title") or "",
                    "source": cached_result.get("source") or "",
                    "reason": cached_result.get("reason") or "result_cache_hit",
                    "meta": cached_meta,
                }
                self.logger.debug("result_cache_hit", station_key=station_key)
            else:
                result = self._resolve_song(station_name, station_id=station_id, station_key=station_key)
            request_ts_parsed = self._parse_request_ts(req_ts)
            result = self._apply_qf_parity_policy(station_key, result, request_ts=request_ts_parsed)
            if (result.get("status") or "") == "hit":
                self._store_cached_result(station_key, result)
            else:
                self._invalidate_cached_result(station_key)
            _finalize_request(
                status=result.get("status") or "error",
                reason=result.get("reason") or "",
                artist=result.get("artist") or "",
                title=result.get("title") or "",
                source=result.get("source") or "",
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
            _finalize_request(
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
