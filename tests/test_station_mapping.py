import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import station_mapping as sm  # noqa: E402


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "sub", "song_data.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _legacy(self, rows):
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE station_uuid_map (station_key TEXT PRIMARY KEY, stationuuid TEXT NOT NULL, updated_ts INTEGER NOT NULL DEFAULT 0)")
        conn.executemany("INSERT INTO station_uuid_map VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()

    def test_legacy_rows_migrate_as_unverified_and_old_table_stays(self):
        self._legacy([("name:radio bob", "uuid-1", 123)])
        m = sm.load_mapping(self.db, "name", "name:radio bob")
        self.assertEqual(m["stationuuid"], "uuid-1")
        self.assertEqual(m["confirmation_source"], sm.CONFIRM_LEGACY)
        self.assertFalse(sm.is_trusted(m))
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT count(*) FROM station_uuid_map").fetchone()[0], 1)
        conn.close()

    def test_migration_is_idempotent_and_keeps_user_choice(self):
        self._legacy([("name:radio bob", "uuid-1", 123)])
        sm.load_mapping(self.db, "name", "name:radio bob")
        sm.save_mapping(self.db, "name", "name:radio bob", "uuid-2", station_name="RADIO BOB! NRW")
        m = sm.load_mapping(self.db, "name", "name:radio bob")  # re-runs ensure_schema
        self.assertEqual((m["stationuuid"], m["confirmation_source"]), ("uuid-2", sm.CONFIRM_USER))
        self.assertTrue(sm.is_trusted(m))

    def test_same_name_different_pvr_channels_are_independent(self):
        sm.save_mapping(self.db, "pvr", "client1:101", "uuid-a", station_name="RADIO BOB!")
        sm.save_mapping(self.db, "pvr", "client1:202", "uuid-b", station_name="RADIO BOB!")
        self.assertEqual(sm.load_mapping(self.db, "pvr", "client1:101")["stationuuid"], "uuid-a")
        self.assertEqual(sm.load_mapping(self.db, "pvr", "client1:202")["stationuuid"], "uuid-b")

    def test_automatic_candidates_cannot_be_persisted(self):
        with self.assertRaises(ValueError):
            sm.save_mapping(self.db, "name", "name:x", "u", confirmation_source=sm.CONFIRM_LEGACY)
        self.assertIsNone(sm.load_mapping(self.db, "name", "name:x"))

    def test_deleted_legacy_row_does_not_come_back(self):
        self._legacy([("name:radio bob", "uuid-1", 123)])
        self.assertIsNotNone(sm.load_mapping(self.db, "name", "name:radio bob"))
        self.assertTrue(sm.delete_mapping(self.db, "name", "name:radio bob"))
        self.assertIsNone(sm.load_mapping(self.db, "name", "name:radio bob"))
        self.assertEqual(sm.list_mappings(self.db), [])

    def test_persists_across_reopen_and_delete(self):
        sm.save_mapping(self.db, "name", "name:x", "u1")
        self.assertEqual(len(sm.list_mappings(self.db)), 1)
        self.assertTrue(sm.delete_mapping(self.db, "name", "name:x"))
        self.assertIsNone(sm.load_mapping(self.db, "name", "name:x"))
        self.assertFalse(sm.delete_mapping(self.db, "name", "name:x"))


if __name__ == "__main__":
    unittest.main()
