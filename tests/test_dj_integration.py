import asyncio
import importlib
import os
import tempfile
import unittest


class TestAIDJIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "dj_test.db")

        os.environ["DJ_DB_PATH"] = self.db_path
        os.environ["DJ_ALGO_VERSION"] = "v2"
        os.environ["DJ_SHADOW_MODE"] = "0"
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

        # Ensure pop marks played and returns one recommendation.
        self.engine._sessions[42].recommendation_buffer = list(recs)
        popped = await self.engine.pop_next_recommendation(42)
        self.assertIsNotNone(popped)
        self.assertIn(" - ", f"{popped.artist.lower()} - {popped.title.lower()}")

        self.engine.stop_session(42)
        self.assertFalse(self.engine.is_active(42))


if __name__ == "__main__":
    unittest.main()
