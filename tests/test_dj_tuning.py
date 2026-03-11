import asyncio
import importlib
import json
import os
import tempfile
import unittest


class TestDJTuning(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "dj_tuning.db")
        os.environ["DJ_DB_PATH"] = self.db_path
        os.environ["DJ_VECTOR_BACKEND"] = "faiss"

        import utils.dj as dj_module

        self.dj_module = importlib.reload(dj_module)

    def tearDown(self):
        os.environ.pop("DJ_RANKER_WEIGHTS_PATH", None)
        asyncio.run(self.dj_module.dj.close())
        self._tmpdir.cleanup()

    def test_centered_ts_bonus_is_neutral_at_half(self):
        self.assertAlmostEqual(self.dj_module._centered_ts_bonus(0.5), 0.0)
        self.assertLess(self.dj_module._centered_ts_bonus(0.1), 0.0)
        self.assertGreater(self.dj_module._centered_ts_bonus(0.9), 0.0)
        self.assertAlmostEqual(self.dj_module._centered_ts_bonus(-3.0), -1.5)
        self.assertAlmostEqual(self.dj_module._centered_ts_bonus(4.0), 1.5)

    def test_cold_start_requires_sparse_seeds_and_history(self):
        self.assertTrue(self.dj_module._is_cold_start(2, 5))
        self.assertFalse(self.dj_module._is_cold_start(3, 5))
        self.assertFalse(self.dj_module._is_cold_start(2, 6))

    def test_runtime_loads_trained_ranker_weights_file(self):
        weights_path = os.path.join(self._tmpdir.name, "weights.json")
        with open(weights_path, "w", encoding="utf-8") as handle:
            json.dump({"weights": {"novelty": 0.03, "completion_prob": 0.52}}, handle)
        os.environ["DJ_RANKER_WEIGHTS_PATH"] = weights_path
        self.dj_module = importlib.reload(self.dj_module)
        self.assertAlmostEqual(self.dj_module.dj._ranker.weights["novelty"], 0.03)
        self.assertAlmostEqual(self.dj_module.dj._ranker.weights["completion_prob"], 0.52)
