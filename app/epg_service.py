"""Best-effort EPG retrieval for radio streams."""

from __future__ import annotations

import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .config import EPG_CANDIDATE_PATHS, EPG_READ_BYTES, EPG_REQUEST_TIMEOUT_SECONDS, USER_AGENT
from .models import EpgInfo


class EpgService:
    def __init__(self, log) -> None:
        self._log = log

    def fetch(self, stream_url: str, homepage_url: str = "") -> EpgInfo:
        hosts = self._collect_hosts(stream_url, homepage_url)
        if not hosts:
            return EpgInfo(
                available=False,
                source_url="",
                summary="Keine Host-Information für EPG-Probe verfügbar.",
                error="missing-host",
            )

        last_error = ""
        max_probes = 4
        probe_count = 0
        deadline = time.monotonic() + 8
        for host in hosts:
            for scheme in ("https", "http"):
                for path in EPG_CANDIDATE_PATHS:
                    if probe_count >= max_probes or time.monotonic() > deadline:
                        return EpgInfo(
                            available=False,
                            source_url="",
                            summary="Kein EPG gefunden (Probe-Limit erreicht).",
                            error=last_error,
                        )
                    candidate_url = f"{scheme}://{host}{path}"
                    self._log(f"EPG-Probe: {candidate_url}")
                    probe_count += 1
                    try:
                        epg_info = self._fetch_one(candidate_url)
                    except Exception as err:
                        last_error = str(err)
                        continue
                    if epg_info.available:
                        return epg_info

        return EpgInfo(
            available=False,
            source_url="",
            summary="Kein EPG gefunden (oder Sender veröffentlicht keines).",
            error=last_error,
        )

    def _fetch_one(self, url: str) -> EpgInfo:
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"})
        timeout_seconds = min(EPG_REQUEST_TIMEOUT_SECONDS, 2)
        with urlopen(req, timeout=timeout_seconds) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            payload = response.read(EPG_READ_BYTES)

        if not payload:
            return EpgInfo(available=False, source_url=url, summary="Leere EPG-Antwort", raw_xml="")

        raw_preview = self._to_text_preview(payload)
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            summary = f"Antwort ist kein valides XML ({content_type or 'unbekannter Content-Type'})"
            return EpgInfo(
                available=False,
                source_url=url,
                summary=summary,
                raw_xml=raw_preview,
            )

        if not self._looks_like_epg_xml(root):
            return EpgInfo(
                available=False,
                source_url=url,
                summary="XML gefunden, aber kein erkennbares EPG/SPI-Dokument",
                raw_xml=raw_preview,
            )

        summary = self._build_summary(root)
        if not summary:
            summary = f"EPG/SPI-XML erkannt ({content_type or 'unbekannter Content-Type'})"

        return EpgInfo(available=True, source_url=url, summary=summary, raw_xml=raw_preview)

    def _build_summary(self, root: ET.Element) -> str:
        programme_names: list[str] = []
        service_names: list[str] = []

        for elem in root.iter():
            tag = self._strip_xml_ns(elem.tag).lower()
            text = (elem.text or "").strip()
            if not text:
                continue

            if tag.endswith("serviceprovidername") or tag.endswith("longname"):
                if text not in service_names:
                    service_names.append(text)

            if tag.endswith("mediumname") or tag.endswith("shortname"):
                if text not in service_names:
                    service_names.append(text)

            if tag.endswith("name") and len(text) > 2:
                if text not in programme_names:
                    programme_names.append(text)

            if len(programme_names) >= 5 and len(service_names) >= 2:
                break

        service_part = ", ".join(service_names[:2]) if service_names else "-"
        programme_part = ", ".join(programme_names[:5]) if programme_names else "-"
        return f"EPG erkannt | Service: {service_part} | Programme: {programme_part}"

    def _collect_hosts(self, stream_url: str, homepage_url: str) -> list[str]:
        hosts = []

        stream_host = urlparse(stream_url).hostname
        if stream_host:
            hosts.append(stream_host)
            if stream_host.count(".") >= 2:
                hosts.append(".".join(stream_host.split(".")[-2:]))

        home_host = urlparse(homepage_url).hostname
        if home_host and home_host not in hosts:
            hosts.append(home_host)
            if home_host.count(".") >= 2:
                base_home = ".".join(home_host.split(".")[-2:])
                if base_home not in hosts:
                    hosts.append(base_home)

        deduped = []
        seen = set()
        for host in hosts:
            if host in seen:
                continue
            seen.add(host)
            deduped.append(host)
        return deduped

    def _looks_like_epg_xml(self, root: ET.Element) -> bool:
        root_tag = self._strip_xml_ns(root.tag).lower()
        if any(token in root_tag for token in ("serviceinformation", "schedule", "epg", "programme", "program")):
            return True

        epg_hints = {
            "service",
            "programme",
            "program",
            "schedule",
            "event",
            "broadcast",
            "bearer",
            "shortname",
            "mediumname",
            "longname",
            "serviceprovidername",
        }
        hits = 0
        for elem in root.iter():
            tag = self._strip_xml_ns(elem.tag).lower()
            if tag in epg_hints:
                hits += 1
                if hits >= 2:
                    return True
        return False

    def _strip_xml_ns(self, tag: str) -> str:
        if "}" in tag:
            return tag.split("}", 1)[1]
        return tag

    def _to_text_preview(self, payload: bytes, limit: int = 15000) -> str:
        if not payload:
            return ""
        sample = payload[: min(len(payload), 4096)]
        printable = 0
        for byte in sample:
            if byte in (9, 10, 13) or 32 <= byte <= 126:
                printable += 1
        if sample and (printable / len(sample)) < 0.75:
            return ""
        return payload.decode("utf-8", errors="replace").strip()[:limit]
