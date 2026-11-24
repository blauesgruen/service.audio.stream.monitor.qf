import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.selection_state import CANCELLED, CHOSEN, PENDING, SelectionRegistry  # noqa: E402


class SelectionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.r = SelectionRegistry()
        self.r.on_request("name:a")

    def test_dialog_requested_only_once_while_polling(self):
        self.assertTrue(self.r.begin_pending("name:a", [{"stationuuid": "1"}]))
        for _ in range(5):
            self.assertEqual(self.r.on_request("name:a"), "")
            self.assertFalse(self.r.begin_pending("name:a", []))
        self.assertEqual(self.r.get("name:a")["state"], PENDING)

    def test_choose_without_save_sticks_for_session_only(self):
        self.r.begin_pending("name:a", [])
        self.assertTrue(self.r.choose("name:a", "u1", save=False))
        self.assertEqual(self.r.get("name:a")["state"], CHOSEN)
        self.assertFalse(self.r.begin_pending("name:a", []))  # not asked again
        self.assertEqual(self.r.on_request("name:a"), "")
        self.assertEqual(self.r.on_request("name:b"), "name:a")  # station change ends session
        self.assertIsNone(self.r.get("name:a"))

    def test_cancel_blocks_reask_until_station_change(self):
        self.r.begin_pending("name:a", [])
        self.assertTrue(self.r.cancel("name:a"))
        self.assertEqual(self.r.get("name:a")["state"], CANCELLED)
        self.assertFalse(self.r.begin_pending("name:a", []))
        self.r.on_request("name:b")
        self.r.on_request("name:a")  # new session: may ask again
        self.assertTrue(self.r.begin_pending("name:a", []))

    def test_stale_dialog_result_is_ignored_after_station_change(self):
        self.r.begin_pending("name:a", [])
        self.r.on_request("name:b")
        self.assertFalse(self.r.choose("name:a", "u1", save=True))
        self.assertFalse(self.r.cancel("name:a"))
        self.assertIsNone(self.r.get("name:a"))

    def test_no_pending_for_inactive_station(self):
        self.assertFalse(self.r.begin_pending("name:zzz", []))

    def test_force_pending_reopens_after_decision_but_not_while_open(self):
        self.r.begin_pending("name:a", [])
        self.assertFalse(self.r.force_pending("name:a", []))  # dialog open
        self.r.choose("name:a", "u1", save=False)
        self.assertTrue(self.r.force_pending("name:a", [{"stationuuid": "u2"}]))  # manual change
        self.assertEqual(self.r.get("name:a")["state"], PENDING)
        self.assertFalse(self.r.force_pending("name:other", []))


if __name__ == "__main__":
    unittest.main()
