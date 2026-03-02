import wavelink
import difflib
import re
from typing import Optional

class TrackMatcher:
    def __init__(self):
        # Keywords that drastically change the version of a song.
        # Must stay in sync with the `noisy` tuple in Music._pick_playable_candidate.
        self.penalty_keywords = [
            "live", "cover", "karaoke", "instrumental", "acoustic",
            "remix", "8d", "slowed", "reverb", "sped up", "nightcore", "tiktok",
        ]

    def sanitize_string(self, text: str) -> str:
        """Removes special characters and lowers case for better comparison."""
        text = text.lower()
        # Remove anything in parentheses or brackets (often contains "feat." or "Official Video")
        text = re.sub(r'[\(\[].*?[\)\]]', '', text)
        # Remove common fluff
        text = re.sub(r'(official video|official audio|lyric video|lyrics|music video)', '', text)
        # Remove non-alphanumeric (keep spaces)
        text = re.sub(r'[^a-z0-9\s]', '', text)
        return text.strip()

    def calculate_similarity(self, a: str, b: str) -> float:
        """Returns a string similarity ratio between 0.0 and 1.0"""
        return difflib.SequenceMatcher(None, a, b).ratio()

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

        # Check if the original track is a special version (so we don't penalize it)
        original_lower = original_title.lower()
        target_is_live = "live" in original_lower
        target_is_remix = "remix" in original_lower
        target_is_acoustic = "acoustic" in original_lower
        target_is_nightcore = "nightcore" in original_lower

        for track in candidates:
            score = 0.0
            
            candidate_title = track.title.lower()
            candidate_title_clean = self.sanitize_string(track.title)
            candidate_author_clean = self.sanitize_string(track.author)

            # 1. Base String Similarity (Title)
            title_sim = self.calculate_similarity(target_title_clean, candidate_title_clean)
            score += (title_sim * 50) # Max 50 points

            # 2. Base String Similarity (Artist/Author)
            author_sim = self.calculate_similarity(target_artist_clean, candidate_author_clean)
            # YouTube Music author often matches well. Standard YouTube often has "Artist - Topic" or just VEVO channels
            if target_artist_clean in candidate_author_clean or candidate_author_clean in target_artist_clean:
                score += 30
            else:
                score += (author_sim * 30) # Max 30 points

            # 3. Exact Match Bonuses
            if target_title_clean == candidate_title_clean:
                score += 15

            # 4. Keyword Penalties
            for kw in self.penalty_keywords:
                if kw in candidate_title:
                    # Only penalize if the original didn't explicitly ask for it
                    if kw == "live" and target_is_live: continue
                    if kw == "remix" and target_is_remix: continue
                    if kw == "acoustic" and target_is_acoustic: continue
                    if kw == "nightcore" and target_is_nightcore: continue
                    
                    score -= 40 # Heavy penalty for playing a cover/karaoke when not requested
                    
            # Update best match
            if score > highest_score:
                highest_score = score
                best_track = track

        return best_track

matcher = TrackMatcher()
