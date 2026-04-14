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
SONG_END_EMPTY_METADATA_CYCLES = 2
SONG_END_ICY_MISSING_TITLE_GRACE_CYCLES = 4
REQUEST_TIMEOUT_SECONDS = 12
STREAM_READ_BYTES = 262144
PLAYLIST_READ_BYTES = 524288
MAX_REDIRECTS = 8
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"

# QF service parity tuning for Kodi bridge behavior.
QF_SERVICE_GUI_PARITY_ENABLED = True
QF_HOLD_SECONDS = 3
QF_HOLD_SECONDS_MAX = 3.0
QF_NO_HIT_CONFIRM = 2
QF_EMPTY_CONFIRM = 2
QF_STALE_FEED_DROP_SECONDS = 180.0
QF_FEED_RETRY_ATTEMPTS = 2
QF_FEED_RETRY_DELAY_SECONDS = 0.35
QF_STATE_MAX_STATIONS = 128
QF_REQUEST_GAP_BUFFER_SECONDS = 2.0
QF_REQUEST_GAP_MAX_SECONDS = 90.0
QF_REQUEST_GAP_USE_CLIENT_TS = True
QF_REQUEST_GAP_EMA_ALPHA = 0.4
QF_PENDING_FEED_CONFIRM_WITHOUT_HISTORY = False
QF_TELEMETRY_ENABLED = True
QF_FASTPATH_VERIFIED_SOURCE_ENABLED = True
QF_VERIFIED_SOURCE_MAX_AGE_SECONDS = 43200
QF_VERIFIED_SOURCE_FEED_FASTPATH_ENABLED = True
QF_VERIFIED_SOURCE_FEED_FASTPATH_MAX_SECONDS = 1.2
QF_PHASE_TIMING_PRECISION = 3
QF_RESULT_CACHE_ENABLED = True
QF_RESULT_CACHE_TTL_SECONDS = 12
QF_STATION_KEY_NAME_FALLBACK_ENABLED = True
QF_STATION_KEY_NAME_FALLBACK_MIN_TOKENS = 2
QF_STATION_KEY_NAME_FALLBACK_MAX_CANDIDATES = 6
QF_SUPERSEDE_PREEMPT_ENABLED = True
QF_SUPERSEDE_MIDFLIGHT_ENABLED = False
QF_DISCOVERY_QUICKPASS_ENABLED = True
QF_DISCOVERY_QUICKPASS_MAX_CANDIDATES = 3
QF_DISCOVERY_QUICKPASS_MAX_SECONDS = 1.2
QF_FEED_RETRY_MIN_ATTEMPTS = 1
QF_FEED_RETRY_MAX_ATTEMPTS = 3
QF_FEED_RETRY_SHORT_GAP_SECONDS = 8.0
QF_FEED_RETRY_LONG_GAP_SECONDS = 25.0
RADIO_BROWSER_LOOKUP_LIMIT = 25
RADIO_BROWSER_SEARCH_LOOKUP_LIMIT = 50
STATION_LOOKUP_MIN_QUERY_LENGTH = 5
STATION_LOOKUP_MIN_TOKENS_PER_VARIANT = 2
STATION_LOOKUP_MAX_QUERY_VARIANTS = 12
STATION_LOOKUP_SEARCH_MAX_QUERY_VARIANTS = 8
STATION_LOOKUP_SEARCH_MIN_TOKEN_LENGTH = 4
STATION_LOOKUP_CHANNEL_FALLBACK_MAX_CHANNELS = 40
STATION_LOOKUP_CHANNEL_FALLBACK_MAX_PAGES = 6
STATION_LOOKUP_CHANNEL_FALLBACK_MIN_SCORE = 900
STATION_LOOKUP_STRICT_MIN_QUERY_TOKENS = 2
STATION_LOOKUP_OPTIONAL_PREFIX_TOKENS = {
    "ard",
    "bbc",
    "br",
    "hr",
    "mdr",
    "ndr",
    "orf",
    "rbb",
    "sr",
    "swr",
    "wdr",
}
STATION_LOOKUP_SIGNIFICANT_SHORT_TOKENS = {
    "dj",
}
STATION_LOOKUP_IGNORED_TOKENS = {
    "radio",
}
STATION_LOOKUP_SKIP_PREFIX_TOKENS = {
    "vom",
    "von",
    "der",
    "die",
    "das",
    "dem",
    "den",
    "des",
    "am",
    "im",
    "mit",
    "und",
    "the",
    "of",
}
STATION_LOOKUP_NUMBER_TOKEN_MAP = {
    "zero": "0",
    "null": "0",
    "one": "1",
    "eins": "1",
    "ein": "1",
    "eine": "1",
    "two": "2",
    "zwei": "2",
    "three": "3",
    "drei": "3",
    "four": "4",
    "vier": "4",
    "five": "5",
    "fuenf": "5",
    "fünf": "5",
    "six": "6",
    "sechs": "6",
    "seven": "7",
    "sieben": "7",
    "eight": "8",
    "acht": "8",
    "nine": "9",
    "neun": "9",
    "ten": "10",
    "zehn": "10",
    "eleven": "11",
    "elf": "11",
    "twelve": "12",
    "zwoelf": "12",
    "zwölf": "12",
}
STATION_LOOKUP_SLUG_MIN_LENGTH = 3
STATION_LOOKUP_MAX_SLUG_VARIANTS = 20

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
NOWPLAYING_STALE_WITHOUT_STREAM_TRACK_MAX_AGE_MINUTES = 12
NOWPLAYING_STRICT_WEBPLAYER_SOURCE = True
NOWPLAYING_QUERY_CONTEXT_IGNORE_TOKENS = {
    "http",
    "https",
    "www",
    "www1",
    "www2",
    "de",
    "com",
    "net",
    "org",
    "radio",
    "stream",
    "live",
    "mp3",
    "aac",
    "ogg",
    "m3u",
    "m3u8",
    "hls",
    "icecast",
    "icecastssl",
    "dispatcher",
    "rndfnk",
    "ard",
    "vom",
    "von",
    "der",
    "die",
    "das",
    "dem",
    "den",
    "des",
    "im",
    "am",
    "und",
    "the",
    "of",
    "in",
}
NOWPLAYING_CANDIDATE_KEYWORDS = (
    "playlist",
    "radiomodul",
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
