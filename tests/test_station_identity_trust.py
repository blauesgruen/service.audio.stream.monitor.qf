"""Offline tests for stream-variant grouping and identity tiers (real Radio-Browser fixtures)."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.station_identity_trust import (  # noqa: E402
    TIER_CONFIRMED,
    TIER_PROBABLE,
    group_stream_variants,
    redact_url,
    same_program_stream,
    stream_channel_key,
)
from app.station_lookup import StationLookupError, StationLookupService  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        payload = json.load(handle)
    return StationLookupService(lambda _m: None)._extract_candidates(payload)


class ChannelKeyTests(unittest.TestCase):
    def test_format_and_player_segments_are_not_channel(self):
        self.assertEqual(stream_channel_key("http://streams.radiobob.de/bob-national/mp3-192/streams.radiobob.de/"), "bob-national")
        self.assertEqual(stream_channel_key("https://streams.radiobob.de/bob-national/aac-64/onlineradiobox/"), "bob-national")
        self.assertEqual(stream_channel_key("http://a.de/bob-national/mp3-192?token=abc"), "bob-national")

    def test_live_is_a_real_channel_token(self):
        self.assertEqual(stream_channel_key("http://streams.radiobob.de/bob-live/mp3-192"), "bob-live")
        self.assertEqual(stream_channel_key("http://streams.radiobob.de/live-nrw-mitte/mp3-192/"), "live-nrw-mitte")

    def test_no_channel_info(self):
        self.assertEqual(stream_channel_key("http://a.de/stream.mp3"), "")

    def test_extension_style_formats(self):
        self.assertEqual(stream_channel_key("http://stream.antenne.de/top-40.aac"), "top-40")
        self.assertEqual(stream_channel_key("http://stream.antenne.de/coffee/stream/aacp"), "coffee")


class ServerSegmentTests(unittest.TestCase):
    def test_cdn_server_segments_are_not_channel(self):
        self.assertEqual(stream_channel_key("https://liveradio.swr.de/sw282p3/swr3/play.mp3"), "swr3")
        self.assertTrue(same_program_stream(
            "https://liveradio.swr.de/sw282p3/swr3/play.mp3",
            "https://liveradio.swr.de/sw331ch/swr3/play.aac",
        ))

    def test_short_channel_ids_stay(self):
        for seg in ("swr3", "wdr2", "1live", "top40", "bayern1"):
            self.assertEqual(stream_channel_key(f"http://a.de/{seg}/mp3-128"), seg)
        self.assertFalse(same_program_stream("http://a.de/swr3/mp3-128", "http://a.de/swr1/mp3-128"))

    def test_antenne_style_paths_group(self):
        self.assertTrue(same_program_stream(
            "http://www.antenne.de/webradio/antenne.aac.pls",
            "https://stream.antenne.de/antenne/stream/mp3",
        ))
        self.assertFalse(same_program_stream(
            "https://stream.antenne.de/antenne/stream/mp3",
            "http://stream.antenne.de/80er-kulthits/stream/aacp",
        ))


class GroupingTests(unittest.TestCase):
    def test_bob_channels_of_one_operator_stay_separate(self):
        for url_a, url_b in [
            ("http://streams.radiobob.de/bob-national/mp3-192", "http://streams.radiobob.de/bob-classicrock/mp3-192"),
            ("http://streams.radiobob.de/bob-national/mp3-192", "http://streams.radiobob.de/bob-live/mp3-192"),
            ("http://streams.radiobob.de/bob-live/mp3-192", "https://streams.radiobob.de/live-nrw-mitte/mp3-192/"),
            ("http://streams.radiobob.de/bob-shlive/mp3-192", "http://streams.radiobob.de/bob-alternative/mp3-192"),
        ]:
            self.assertFalse(same_program_stream(url_a, url_b), (url_a, url_b))

    def test_bob_formats_of_one_channel_group(self):
        self.assertTrue(same_program_stream(
            "http://streams.radiobob.de/bob-national/mp3-192/",
            "https://streams.radiobob.de/bob-national/aac-64/onlineradiobox/",
        ))

    def test_different_operator_same_key_not_grouped(self):
        self.assertFalse(same_program_stream("http://a.example.de/live/mp3-128", "http://b.other.com/live/mp3-128"))

    def test_unclear_stays_separate(self):
        self.assertFalse(same_program_stream("http://a.de/stream.mp3", "http://a.de/other.mp3"))
        self.assertTrue(same_program_stream("http://a.de/stream.mp3", "http://a.de/stream.mp3"))

    def test_fixture_metallica_formats_form_one_group(self):
        metallica = [s for s in _load("radiobrowser_radio_bob.json") if "metallica" in s.name.lower()]
        self.assertGreaterEqual(len(metallica), 3)
        self.assertEqual(len(group_stream_variants(metallica)), 1)

    def test_fixture_distinct_channels_not_merged(self):
        stations = _load("radiobrowser_radio_bob_excl.json")
        keys = {}
        for s in stations:
            keys.setdefault(stream_channel_key(s.stream_url), []).append(s)
        groups = group_stream_variants(stations)
        for key in ("bob-live", "live-nrw-mitte", "bob-shlive"):
            self.assertIn(key, keys)
        for group in groups:
            self.assertEqual(len({stream_channel_key(s.stream_url) for s in group}), 1)


class RedactTests(unittest.TestCase):
    def test_query_and_userinfo_removed(self):
        self.assertEqual(
            redact_url("https://user:pw@host.de:8000/a/b.mp3?sABC=secret&token=1#frag"),
            "https://host.de:8000/a/b.mp3",
        )


class TierTests(unittest.TestCase):
    def setUp(self):
        self.lookup = StationLookupService(lambda _m: None)
        self.lookup._fetch_sibling_candidates = lambda _q: []  # no network in tests
        self.stations = _load("radiobrowser_radio_bob_excl.json")

    def _assess(self, query, selected, pool):
        self.lookup._last_candidates = list(pool)
        self.lookup._apply_identity_assessment(selected, query)
        return selected

    def test_exact_name_unique_program_is_confirmed_option_a(self):
        stations = _load("radiobrowser_radio_bob.json")
        selected = next(s for s in stations if s.name == "RADIO BOB Metal")
        self._assess("RADIO BOB Metal", selected, stations)
        self.assertEqual(selected.identity_tier, TIER_CONFIRMED, selected.identity_candidates)
        self.assertEqual(selected.identity_reasons, ["exact_name_unique_program"])

    def test_regional_siblings_make_exact_name_probable(self):
        stations = _load("radiobrowser_radio_bob.json")
        selected = next(s for s in stations if s.name == "RADIO BOB")
        self._assess("RADIO BOB!", selected, stations)
        self.assertEqual(selected.identity_tier, TIER_PROBABLE)
        names = [c["name"] for c in selected.identity_candidates]
        self.assertIn("Radio Bob! Livestream Hessen", names)
        self.assertEqual(selected.identity_candidates[0]["stationuuid"], selected.stationuuid)
        keys = [c["channel_key"] for c in selected.identity_candidates]
        self.assertEqual(len(keys), len(set(keys)), "one candidate per distinct program")

    def test_genre_extensions_do_not_count_as_competitors(self):
        stations = _load("radiobrowser_radio_bob.json")
        selected = next(s for s in stations if s.name == "RADIO BOB")
        self._assess("RADIO BOB!", selected, [s for s in stations if not self.lookup._is_regional_sibling(s, "RADIO BOB!")])
        self.assertEqual(selected.identity_tier, TIER_CONFIRMED)

    def test_manual_candidates_list_one_entry_per_program_exact_and_regional_first(self):
        stations = _load("radiobrowser_radio_bob.json")
        def fake_untiered(q):
            self.lookup._last_candidates = list(stations)  # real method records its pool
            return next(s for s in stations if s.name == "RADIO BOB")

        self.lookup._find_best_match_untiered = fake_untiered
        self.lookup._fetch_sibling_candidates = lambda q: []
        result = self.lookup.collect_program_candidates("RADIO BOB!")
        keys = [c["channel_key"] for c in result]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(result[0]["name"], "RADIO BOB")
        self.assertTrue(any("Hessen" in c["name"] for c in result))
        self.assertLessEqual(len(result), 12)

    def _mk(self, name, url, uuid):
        from app.models import StationMatch
        return StationMatch(uuid, name, url, "", "Germany", "", "MP3", 128, 10, 1)

    def test_query_terms_missing_in_selected_name_make_it_probable(self):
        starfm = self._mk("StarFM Berlin", "https://stream.starfm.de/berlin/mp3-192/iradio", "u-berlin")
        maxrock = self._mk("Star FM Maximum Rock | National", "https://stream.starfm.de/national/mp3-192/iradio", "u-nat")
        self.lookup._fetch_sibling_candidates = lambda q: []
        self.lookup._last_candidates = [starfm, maxrock]
        self.lookup._apply_identity_assessment(starfm, "STAR FM MAXIMUM ROCK! (Berlin)")
        self.assertEqual(starfm.identity_tier, TIER_PROBABLE)
        self.assertTrue(starfm.identity_reasons[0].startswith("query_terms_not_covered:"))
        self.assertEqual(starfm.identity_candidates[0]["stationuuid"], "u-berlin")
        self.assertIn("u-nat", [c["stationuuid"] for c in starfm.identity_candidates])

    def test_number_words_and_format_tokens_do_not_count_as_uncovered(self):
        eins = self._mk("Radio Eins", "http://x.de/radioeins/mp3-128", "u1")
        self.assertEqual(self.lookup._uncovered_query_terms(eins, "Radio Eins"), [])
        self.assertEqual(self.lookup._uncovered_query_terms(eins, "Radio 1"), [])
        dlf = self._mk("Deutschlandfunk", "http://x.de/dlf/mp3-128", "u2")
        self.assertEqual(self.lookup._uncovered_query_terms(dlf, "Deutschlandfunk MP3 128k"), [])

    def test_variants_only_gives_confirmed(self):
        metallica = [s for s in _load("radiobrowser_radio_bob.json") if "metallica" in s.name.lower()]
        self._assess("Radio BOB Metallica", metallica[0], metallica)
        self.assertEqual(metallica[0].identity_tier, TIER_CONFIRMED)

    def test_no_competitors_is_confirmed(self):
        selected = self.stations[0]
        self._assess("Radio BOB", selected, [])
        self.assertEqual(selected.identity_tier, TIER_CONFIRMED)

    def _same_name_pair(self, homepage_b="https://www.radiobob.de/"):
        by_key = {stream_channel_key(s.stream_url): s for s in self.stations}
        a, b = by_key["bob-live"], by_key["live-nrw-mitte"]
        for s in (a, b):
            s.name = "RADIO BOB!"
        a.homepage = "https://www.radiobob.de/"
        b.homepage = homepage_b
        return a, b

    def test_same_name_same_homepage_different_channel_is_only_probable(self):
        a, b = self._same_name_pair()
        selected = self.lookup._select_semantic_candidate([a, b], "RADIO BOB!")
        self.assertEqual(selected.identity_tier, TIER_PROBABLE)
        self.assertIn("same_operator_program_unproven", selected.match_evidence)
        self.assertEqual({c["channel_key"] for c in selected.identity_candidates}, {"bob-live", "live-nrw-mitte"})

    def test_same_name_different_operator_is_ambiguous_tier_c(self):
        a, b = self._same_name_pair(homepage_b="https://other-operator.example/")
        with self.assertRaises(StationLookupError) as ctx:
            self.lookup._select_semantic_candidate([a, b], "RADIO BOB!")
        self.assertEqual(ctx.exception.error_class, "station_ambiguous")
        self.assertEqual(ctx.exception.details["identity_tier"], "C")
        self.assertEqual(len(ctx.exception.details["match_candidates"]), 2)

    def test_same_name_formats_of_one_channel_resolve(self):
        by_key = {}
        for s in self.stations:
            by_key.setdefault(stream_channel_key(s.stream_url), []).append(s)
        pair = next(v for v in by_key.values() if len(v) >= 2)[:2]
        for s in pair:
            s.name = "RADIO BOB! Test"
        selected = self.lookup._select_semantic_candidate(pair, "RADIO BOB! Test")
        self.assertIn(selected, pair)


if __name__ == "__main__":
    unittest.main()
