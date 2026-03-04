import unicodedata
import wavelink
import re
from typing import Optional

from rapidfuzz import fuzz as rfuzz
from rapidfuzz.distance import JaroWinkler
from unidecode import unidecode


class TrackMatcher:
    def __init__(self):
        pass

    @staticmethod
    def sanitize_string(text: str) -> str:
        """Normalizes text with NFKD Unicode decomposition and strips noise."""
        text = text.lower()
        text = text.replace("\u2019", "'").replace("\u2018", "'")
        # NFKD: decompose accented characters, then strip combining marks
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        text = unicodedata.normalize("NFC", text)
        # Remove parenthetical/bracketed content (feat., Official Video, etc.)
        text = re.sub(r'[\(\[].*?[\)\]]', '', text)
        # Remove common YouTube fluff
        text = re.sub(
            r'\b(official|video|audio|lyrics?|lyric|music video|topic|vevo|visualizer|remastered|remaster|hd|4k|hq)\b',
            '', text,
        )
        # Keep Unicode word characters (letters from any script + digits) and spaces
        text = re.sub(r'[^\w\s]', ' ', text, flags=re.UNICODE)
        text = re.sub(r'_', ' ', text)
        return " ".join(text.split())

    @staticmethod
    def transliterate(text: str) -> str:
        """Fallback: transliterate to ASCII for cross-script matching."""
        sanitized = TrackMatcher.sanitize_string(text)
        if not sanitized:
            return ""
        return unidecode(sanitized).lower().strip()

    @staticmethod
    def calculate_similarity(a: str, b: str) -> float:
        """Returns a string similarity ratio between 0.0 and 1.0 using rapidfuzz."""
        if not a or not b:
            return 0.0
        return rfuzz.ratio(a, b) / 100.0

    @staticmethod
    def token_sort_similarity(a: str, b: str) -> float:
        """Word-order-independent fuzzy similarity."""
        if not a or not b:
            return 0.0
        return rfuzz.token_sort_ratio(a, b) / 100.0

    @staticmethod
    def _split_artist_tokens(raw: str) -> set[str]:
        text = TrackMatcher.sanitize_string(raw)
        if not text:
            return set()
        parts = re.split(r'\b(?:feat|ft|featuring|with|and|x)\b|[,&/+]', text)
        return {p.strip() for p in parts if p and len(p.strip()) > 1}

    @staticmethod
    def artist_similarity(expected: str, actual: str) -> float:
        """Jaro-Winkler-enhanced artist matching with token splitting."""
        exp_tokens = TrackMatcher._split_artist_tokens(expected)
        act_tokens = TrackMatcher._split_artist_tokens(actual)
        if not exp_tokens or not act_tokens:
            return 0.0
        # Exact Jaccard
        exact_overlap = len(exp_tokens & act_tokens)
        jaccard = exact_overlap / len(exp_tokens | act_tokens)
        if jaccard >= 0.9:
            return jaccard
        # Fuzzy matching per token with Jaro-Winkler
        fuzzy_matches = 0
        for exp_tok in exp_tokens:
            best = max(
                (JaroWinkler.similarity(exp_tok, act_tok) for act_tok in act_tokens),
                default=0.0,
            )
            if best >= 0.88:
                fuzzy_matches += 1
        fuzzy_score = fuzzy_matches / max(len(exp_tokens), len(act_tokens), 1)
        return max(jaccard, fuzzy_score)

    def get_best_match(self, original_title: str, original_artist: str, candidates: list[wavelink.Playable]) -> Optional[wavelink.Playable]:
        """
        Scores a list of wavelink Playables against the target Spotify title and artist.
        Returns the single best Playable.
        """
        if not candidates:
            return None

        best_track = None
        highest_score = -999.0

        target_title_clean = self.sanitize_string(original_title)
        target_artist_clean = self.sanitize_string(original_artist)

        for track in candidates:
            score = 0.0

            candidate_title_clean = self.sanitize_string(track.title)
            candidate_author_clean = self.sanitize_string(track.author)

            # 1. Sequence-level similarity (rapidfuzz)
            title_sim = self.calculate_similarity(target_title_clean, candidate_title_clean)
            score += title_sim * 35

            # 2. Token-sort similarity (handles word order differences)
            title_sort_sim = self.token_sort_similarity(target_title_clean, candidate_title_clean)
            score += title_sort_sim * 15

            # 3. Artist matching (Jaro-Winkler enhanced)
            artist_sim = self.artist_similarity(original_artist, track.author)
            if target_artist_clean in candidate_author_clean or candidate_author_clean in target_artist_clean:
                score += 30
            else:
                score += artist_sim * 30

            # 4. Exact match bonus
            if target_title_clean == candidate_title_clean:
                score += 15

            if score > highest_score:
                highest_score = score
                best_track = track

        return best_track


matcher = TrackMatcher()
