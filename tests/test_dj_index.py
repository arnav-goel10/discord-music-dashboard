import unittest

from utils.dj_index import TextEmbedder


class TestTextEmbedder(unittest.IsolatedAsyncioTestCase):
    async def test_hash_mode_does_not_require_sentence_transformers(self):
        embedder = TextEmbedder(dim=128, model_name="hash")
        vec = await embedder.embed("The Weeknd - Blinding Lights")
        self.assertEqual(len(vec), 128)
        self.assertIsNone(embedder._model)
        self.assertFalse(embedder._model_failed)


if __name__ == "__main__":
    unittest.main()
