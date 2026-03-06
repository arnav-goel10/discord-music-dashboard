import unittest

from utils.dj_models import ContextualRanker, LinUCBBanditPolicy, RankedCandidate, SlateReranker


class TestDJModels(unittest.TestCase):
    def test_ranker_monotonicity(self):
        ranker = ContextualRanker()
        good = RankedCandidate(
            track_id="a",
            title="Good Track",
            artist="Artist 1",
            retrieval_score=0.9,
            rank_features={
                "completion_prob": 0.9,
                "skip_risk": 0.1,
                "novelty": 0.4,
                "session_fit": 0.8,
                "artist_affinity": 0.6,
                "semantic_similarity": 0.8,
                "sequential_affinity": 0.7,
                "co_listen_strength": 0.7,
                "rule_affinity": 0.5,
                "recency_penalty": 0.0,
                "artist_repetition_penalty": 0.0,
                "resolver_confidence": 0.9,
            },
        )
        poor = RankedCandidate(
            track_id="b",
            title="Poor Track",
            artist="Artist 2",
            retrieval_score=0.3,
            rank_features={
                "completion_prob": 0.2,
                "skip_risk": 0.8,
                "novelty": 0.2,
                "session_fit": 0.2,
                "artist_affinity": 0.1,
                "semantic_similarity": 0.2,
                "sequential_affinity": 0.1,
                "co_listen_strength": 0.1,
                "rule_affinity": 0.1,
                "recency_penalty": 0.8,
                "artist_repetition_penalty": 1.0,
                "resolver_confidence": 0.3,
            },
        )
        ranked = ranker.score([poor, good])
        self.assertEqual(ranked[0].track_id, "a")
        self.assertGreater(ranked[0].rank_score, ranked[1].rank_score)

    def test_bandit_skip_reaction(self):
        policy = LinUCBBanditPolicy()
        row = RankedCandidate(
            track_id="x",
            title="X",
            artist="A",
            rank_score=50.0,
            rank_features={"uncertainty": 0.8, "novelty": 0.8, "skip_risk": 0.2},
        )
        low = policy.apply([RankedCandidate(**row.__dict__)], base_alpha=0.2, session_skip_streak=0, session_completion_streak=0, source_entropy=0.1)[0]
        high = policy.apply([RankedCandidate(**row.__dict__)], base_alpha=0.2, session_skip_streak=4, session_completion_streak=0, source_entropy=0.1)[0]
        self.assertGreater(high.exploration_bonus, low.exploration_bonus)

    def test_slate_diversity_and_duplicate_filter(self):
        reranker = SlateReranker(mmr_lambda=0.65)
        rows = [
            RankedCandidate(track_id="1", title="Song One", artist="Artist A", rank_score=98.0),
            RankedCandidate(track_id="2", title="Song One (Live)", artist="Artist A", rank_score=97.0),
            RankedCandidate(track_id="3", title="Song Two", artist="Artist B", rank_score=96.0),
            RankedCandidate(track_id="4", title="Song Three", artist="Artist C", rank_score=95.0),
            RankedCandidate(track_id="5", title="Song Four", artist="Artist D", rank_score=94.0),
            RankedCandidate(track_id="6", title="Song Five", artist="Artist E", rank_score=93.0),
        ]
        out = reranker.rerank(rows, max_results=5)
        self.assertEqual(len(out), 5)
        first_five_artists = [r.artist for r in out[:5]]
        self.assertEqual(len(first_five_artists), len(set(first_five_artists)))
        # Duplicate-ish title should not survive when alternatives exist.
        ids = {r.track_id for r in out}
        self.assertFalse({"1", "2"}.issubset(ids))


if __name__ == "__main__":
    unittest.main()
