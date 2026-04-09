"""Central app configuration."""

from pathlib import Path

APP_NAME = "Radio Source Finder"
APP_VERSION = "1.0.0"
ORIGIN_ONLY_MODE = True
ALLOW_OFFICIAL_CHAIN_SOURCES = True

NON_ORIGIN_DIRECTORY_BASE_DOMAINS = {
    "radio.de",
    "radio.net",
    "radio.at",
    "radio.fr",
    "radio.es",
    "radio.it",
    "radio.dk",
    "radio.se",
    "radio.pl",
    "radio.pt",
    "radio.co",
    "radio.mx",
    "radio.ca",
    "radio.nz",
    "radio.ie",
    "radio.za",
    "radio.au",
    "radio.br",
    "radio.nl",
}

NON_ORIGIN_ASSET_BASE_DOMAINS = {
    "radio-assets.com",
}

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "radio_sources.db"

UI_POLL_INTERVAL_MS = 100
SONG_REFRESH_INTERVAL_SECONDS = 15
SONG_CLEAR_EMPTY_CYCLES = 2
REQUEST_TIMEOUT_SECONDS = 12
STREAM_READ_BYTES = 262144
PLAYLIST_READ_BYTES = 524288
MAX_REDIRECTS = 8
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
RADIO_BROWSER_LOOKUP_LIMIT = 25

RADIO_BROWSER_BASE_URLS = [
    "http://de1.api.radio-browser.info",
    "http://fr1.api.radio-browser.info",
    "http://nl1.api.radio-browser.info",
]

EPG_REQUEST_TIMEOUT_SECONDS = 6
EPG_READ_BYTES = 1048576
EPG_SEARCH_DEFAULT_ENABLED = False
EPG_CANDIDATE_PATHS = [
    "/radiodns/spi/3.1/SI.xml",
    "/spi/3.1/SI.xml",
    "/SI.xml",
]

DISCOVERY_READ_BYTES = 700000
DISCOVERY_MAX_CANDIDATES = 15
DISCOVERY_REQUEST_TIMEOUT_SECONDS = 2
MAX_NOWPLAYING_AGE_MINUTES = 45
NOWPLAYING_DURATION_GRACE_SECONDS = 45
NOWPLAYING_CANDIDATE_KEYWORDS = (
    "playlist",
    "titelliste",
    "playout",
    "onair",
    "nowplaying",
    "now-next",
    "current",
    "now",
    "song",
    "track",
    "titel",
    "title",
    "artist",
    "stream",
    "metadata",
)

SUPPORTED_PLAYLIST_CONTENT_TYPES = {
    "audio/x-mpegurl",
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "audio/mpegurl",
    "audio/x-scpls",
    "application/pls+xml",
    "application/xspf+xml",
    "application/vnd.apple.mpegurl.audio",
}

SUPPORTED_PLAYLIST_SUFFIXES = {
    ".m3u",
    ".m3u8",
    ".pls",
    ".xspf",
}
