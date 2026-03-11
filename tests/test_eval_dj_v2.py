import json
import sqlite3
import tempfile
import time
import unittest

from scripts import eval_dj_v2


class TestEvalDJV2(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = f"{self._tmpdir.name}/eval.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE dj_recs_audit_v2 (
                    event_id TEXT PRIMARY KEY,
                    ranked_json TEXT NOT NULL,
                    chosen_track_id TEXT,
                    outcome_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE TABLE dj_track_catalog_v2 (
                    track_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO dj_track_catalog_v2(track_id, title, artist) VALUES (?, ?, ?)",
                [
                    ("t1", "Song 1", "Artist 1"),
                    ("t2", "Song 2", "Artist 2"),
                    ("t3", "Song 3", "Artist 3"),
                    ("t4", "Song 4", "Artist 2"),
                    ("unused", "Unused Song", "Unused Artist"),
                ],
            )
            conn.execute(
                """
                INSERT INTO dj_recs_audit_v2(event_id, ranked_json, chosen_track_id, outcome_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "row1",
                    json.dumps(
                        [
                            {
                                "track_id": "t1",
                                "title": "Song 1",
                                "artist": "Artist 1",
                                "rank_features": {"novelty": 0.9},
                            },
                            {
                                "track_id": "t2",
                                "title": "Song 2",
                                "artist": "Artist 2",
                                "rank_features": {"novelty": 0.6},
                            },
                            {
                                "track_id": "t3",
                                "title": "Song 3",
                                "artist": "Artist 3",
                                "rank_features": {"novelty": 0.4},
                            },
                        ]
                    ),
                    "t2",
                    json.dumps({"events": [{"event_type": "skip", "skip_severity": 0.8}]}),
                    time.time(),
                ),
            )
            conn.execute(
                """
                INSERT INTO dj_recs_audit_v2(event_id, ranked_json, chosen_track_id, outcome_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "row2",
                    json.dumps(
                        [
                            {
                                "track_id": "t2",
                                "title": "Song 2",
                                "artist": "Artist 2",
                                "rank_features": {"novelty": 0.7},
                            },
                            {
                                "track_id": "t4",
                                "title": "Song 4",
                                "artist": "Artist 2",
                                "rank_features": {"novelty": 0.5},
                            },
                            {
                                "track_id": "t3",
                                "title": "Song 3",
                                "artist": "Artist 4",
                                "rank_features": {"novelty": 0.2},
                            },
                        ]
                    ),
                    "t2",
                    json.dumps({"events": [{"event_type": "complete"}]}),
                    time.time() - 1,
                ),
            )
            conn.commit()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_evaluate_reports_ranking_and_beyond_accuracy_metrics(self):
        metrics = eval_dj_v2.evaluate(self.db_path, k=2)

        self.assertEqual(metrics["samples"], 2)
        self.assertEqual(metrics["catalog_size"], 4)
        self.assertEqual(metrics["live_catalog_size"], 5)
        self.assertAlmostEqual(metrics["hit_rate_at_k"], 1.0)
        self.assertAlmostEqual(metrics["ndcg_at_k"], (1.0 + (1.0 / 1.584962500721156)) / 2.0)
        self.assertAlmostEqual(metrics["mrr_at_k"], 0.75)
        self.assertAlmostEqual(metrics["skip_at_1"], 0.5)
        self.assertAlmostEqual(metrics["early_skip_rate_at_1"], 0.5)
        self.assertAlmostEqual(metrics["artist_diversity_at_k"], 0.75)
        self.assertAlmostEqual(metrics["artist_repetition_rate_at_k"], 0.25)
        self.assertAlmostEqual(metrics["mean_novelty_at_k"], 0.675)
        self.assertAlmostEqual(metrics["long_tail_share_at_k"], 0.75)
        self.assertAlmostEqual(metrics["catalog_coverage_at_k"], 0.75)

    def test_evaluate_reconstructs_track_id_when_missing_from_ranked_json(self):
        with sqlite3.connect(self.db_path) as conn:
            chosen = eval_dj_v2._track_id("Song Missing", "Artist Missing")
            conn.execute("DELETE FROM dj_recs_audit_v2")
            conn.execute(
                """
                INSERT INTO dj_recs_audit_v2(event_id, ranked_json, chosen_track_id, outcome_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "row3",
                    json.dumps(
                        [
                            {
                                "title": "Song Missing",
                                "artist": "Artist Missing",
                                "rank_features": {"novelty": 0.8},
                            }
                        ]
                    ),
                    chosen,
                    json.dumps({"events": [{"event_type": "complete"}]}),
                    time.time(),
                ),
            )
            conn.commit()

        metrics = eval_dj_v2.evaluate(self.db_path, k=1)
        self.assertEqual(metrics["samples"], 1)
        self.assertAlmostEqual(metrics["hit_rate_at_k"], 1.0)
        self.assertAlmostEqual(metrics["mrr_at_k"], 1.0)

    def test_evaluate_backfills_missing_track_id_from_unique_catalog_match(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM dj_recs_audit_v2")
            conn.execute(
                """
                INSERT INTO dj_recs_audit_v2(event_id, ranked_json, chosen_track_id, outcome_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "row4",
                    json.dumps(
                        [
                            {
                                "title": "Song 1",
                                "artist": "Artist 1",
                                "rank_features": {"novelty": 0.8},
                            }
                        ]
                    ),
                    "t1",
                    json.dumps({"events": [{"event_type": "complete"}]}),
                    time.time(),
                ),
            )
            conn.commit()

        metrics = eval_dj_v2.evaluate(self.db_path, k=1)
        self.assertEqual(metrics["samples"], 1)
        self.assertAlmostEqual(metrics["hit_rate_at_k"], 1.0)
        self.assertAlmostEqual(metrics["catalog_coverage_at_k"], 1.0)
