import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest


class TestAIDJIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "dj_test.db")

        os.environ["DJ_DB_PATH"] = self.db_path
        os.environ["DJ_VECTOR_BACKEND"] = "faiss"
        os.environ["DJ_CANDIDATE_POOL_SIZE"] = "40"
        os.environ["DJ_RANK_TOPK"] = "20"
        os.environ["DJ_BUFFER_SIZE"] = "6"

        import utils.dj as dj_module

        self.dj_module = importlib.reload(dj_module)
        self.engine = self.dj_module.AIDJ()

    async def asyncTearDown(self):
        await self.engine.close()
        self._tmpdir.cleanup()

    async def test_session_lifecycle_and_v2_recs(self):
        session = self.engine.start_session(
            42,
            seed=self.dj_module.SeedTrack(title="Blinding Lights", artist="The Weeknd", duration_ms=200000),
        )
        self.assertTrue(session.active)

        # Seed catalog and behavior stats.
        await self.engine.record_listen(
            guild_id=42,
            user_id=1001,
            title="Blinding Lights",
            artist="The Weeknd",
            duration_ms=200000,
            listened_ms=180000,
            was_skipped=False,
            source="user_queued",
        )

        async def fake_retrieve(_session, _k):
            return [
                {
                    "track_id": "t1",
                    "title": "Save Your Tears",
                    "artist": "The Weeknd",
                    "source": "semantic_ann",
                    "reason": "Semantic match",
                    "candidate_sources": ["semantic_ann"],
                    "retrieval_score": 0.9,
                    "semantic_score": 0.95,
                    "sequential_score": 0.4,
                    "graph_score": 0.3,
                    "rule_score": 0.2,
                    "resolver_confidence": 0.8,
                },
                {
                    "track_id": "t2",
                    "title": "Levitating",
                    "artist": "Dua Lipa",
                    "source": "graph",
                    "reason": "Graph pick",
                    "candidate_sources": ["graph"],
                    "retrieval_score": 0.8,
                    "semantic_score": 0.6,
                    "sequential_score": 0.6,
                    "graph_score": 0.8,
                    "rule_score": 0.2,
                    "resolver_confidence": 0.7,
                },
                {
                    "track_id": "t3",
                    "title": "As It Was",
                    "artist": "Harry Styles",
                    "source": "lastfm",
                    "reason": "Rule pick",
                    "candidate_sources": ["lastfm"],
                    "retrieval_score": 0.7,
                    "semantic_score": 0.4,
                    "sequential_score": 0.2,
                    "graph_score": 0.2,
                    "rule_score": 0.9,
                    "resolver_confidence": 0.6,
                },
            ]

        self.engine._retriever.retrieve = fake_retrieve

        recs = await self.engine.get_recommendations(42, count=3)
        self.assertEqual(len(recs), 3)
        self.assertTrue(all(r.model_version == "v2" for r in recs))
        self.assertTrue(all(r.track_id for r in recs))

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT ranked_json FROM dj_recs_audit_v2 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        ranked_payload = self.dj_module.json.loads(str(row["ranked_json"] or "[]"))
        self.assertTrue(all(item.get("track_id") for item in ranked_payload if isinstance(item, dict)))

        # Ensure pop marks played and returns one recommendation.
        self.engine._sessions[42].recommendation_buffer = list(recs)
        popped = await self.engine.pop_next_recommendation(42)
        self.assertIsNotNone(popped)
        self.assertIn(" - ", f"{popped.artist.lower()} - {popped.title.lower()}")

        self.engine.stop_session(42)
        self.assertFalse(self.engine.is_active(42))

    async def test_explicit_skip_session_mutates_once_with_update_guard(self):
        self.engine.start_session(
            77,
            seed=self.dj_module.SeedTrack(title="Never Gonna Give You Up", artist="Rick Astley", duration_ms=210000),
        )
        session = self.engine.get_session(77)
        assert session is not None

        await self.engine.record_listen(
            guild_id=77,
            user_id=2002,
            title="Take On Me",
            artist="a-ha",
            duration_ms=220000,
            listened_ms=8000,
            was_skipped=True,
            skip_position_ms=8000,
            source="ai_dj",
            update_session=False,
        )
        self.assertEqual(session.skip_streak, 0)
        self.assertEqual(session.completion_streak, 0)

        self.engine.on_skip(
            77,
            title="Take On Me",
            artist="a-ha",
            listened_ms=8000,
            duration_ms=220000,
        )
        self.assertEqual(session.skip_streak, 1)
        self.assertEqual(session.completion_streak, 0)
        self.assertIn("a-ha - take on me", session.skipped_titles)

    async def test_per_recommendation_audit_mapping_survives_interleaving(self):
        self.engine.start_session(
            88,
            seed=self.dj_module.SeedTrack(title="Seed Song", artist="Seed Artist", duration_ms=180000),
        )

        batches = [
            [self.dj_module.RecommendedTrack(title="Song A", artist="Artist A", score=1.0)],
            [self.dj_module.RecommendedTrack(title="Song B", artist="Artist B", score=0.9)],
        ]

        async def fake_get_recs(_guild_id: int, *, count: int):
            if not batches:
                return []
            return list(batches.pop(0))[:count]

        self.engine._get_recommendations_v2 = fake_get_recs

        recs_a = await self.engine.get_recommendations(88, count=1)
        recs_b = await self.engine.get_recommendations(88, count=1)

        self.assertEqual(len(recs_a), 1)
        self.assertEqual(len(recs_b), 1)
        self.assertTrue(recs_a[0].audit_event_id)
        self.assertTrue(recs_b[0].audit_event_id)
        self.assertNotEqual(recs_a[0].audit_event_id, recs_b[0].audit_event_id)

        session = self.engine.get_session(88)
        assert session is not None
        session.recommendation_buffer = [recs_a[0], recs_b[0]]

        popped_a = await self.engine.pop_next_recommendation(88)
        popped_b = await self.engine.pop_next_recommendation(88)
        assert popped_a is not None
        assert popped_b is not None

        chosen_a = self.engine._track_id(popped_a.title, popped_a.artist)
        chosen_b = self.engine._track_id(popped_b.title, popped_b.artist)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT event_id, chosen_track_id
                FROM dj_recs_audit_v2
                WHERE event_id IN (?, ?)
                """,
                (recs_a[0].audit_event_id, recs_b[0].audit_event_id),
            ).fetchall()
        by_event = {str(row["event_id"]): str(row["chosen_track_id"] or "") for row in rows}
        self.assertEqual(by_event.get(recs_a[0].audit_event_id), chosen_a)
        self.assertEqual(by_event.get(recs_b[0].audit_event_id), chosen_b)

    async def test_pop_next_recommendation_survives_audit_mark_failure(self):
        self.engine.start_session(
            99,
            seed=self.dj_module.SeedTrack(title="Seed", artist="Artist", duration_ms=120000),
        )
        session = self.engine.get_session(99)
        assert session is not None
        rec = self.dj_module.RecommendedTrack(
            title="Failure Safe",
            artist="Audit Guard",
            score=0.5,
            audit_event_id="broken-audit-row",
        )
        session.recommendation_buffer = [rec]

        async def _explode(_event_id: str, _rec):
            raise RuntimeError("simulated audit write failure")

        self.engine._mark_audit_chosen = _explode
        popped = await self.engine.pop_next_recommendation(99)
        self.assertIsNotNone(popped)
        self.assertEqual(popped.title, "Failure Safe")
        self.assertEqual(len(session.recommendation_buffer), 0)


if __name__ == "__main__":
    unittest.main()
