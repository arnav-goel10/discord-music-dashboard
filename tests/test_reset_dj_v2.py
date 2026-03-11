import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import reset_dj_v2


class TestResetDJV2(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "reset.db")
        self.snapshot_path = os.path.join(self._tmpdir.name, "faiss_tracks_v2.index")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE dj_embeddings (entity_type TEXT, entity_id TEXT, embedding BLOB, updated_at REAL)")
            conn.execute("INSERT INTO dj_embeddings VALUES ('guild', '1', X'00', 1.0)")
            conn.commit()
        with open(self.snapshot_path, "w", encoding="utf-8") as handle:
            handle.write("snapshot")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_reset_clears_tables_and_removes_snapshot(self):
        summary = reset_dj_v2.reset_dj_state(self.db_path, snapshot_path=self.snapshot_path)
        self.assertTrue(summary["snapshot_removed"])
        self.assertFalse(os.path.exists(self.snapshot_path))
        with sqlite3.connect(self.db_path) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM dj_embeddings").fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_reset_stages_snapshot_if_delete_fails(self):
        with patch("scripts.reset_dj_v2.os.remove", side_effect=OSError("cannot delete")):
            summary = reset_dj_v2.reset_dj_state(self.db_path, snapshot_path=self.snapshot_path)
        self.assertFalse(summary["snapshot_removed"])
        self.assertFalse(os.path.exists(self.snapshot_path))
        self.assertTrue(os.path.exists(summary["staged_snapshot_path"]))
        with sqlite3.connect(self.db_path) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM dj_embeddings").fetchone()[0]
        self.assertEqual(remaining, 0)
