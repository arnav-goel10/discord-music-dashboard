#!/usr/bin/env python3
"""Demonstrate the AI DJ offline evaluation harness on synthetic data.

Builds a deterministic, seeded audit database in the same schema the bot
writes at runtime (dj_recs_audit_v2 + dj_track_catalog_v2), then runs the
real scripts/eval_dj_v2.evaluate() over it.

The numbers this prints characterize the HARNESS, not the recommender:
every listening event here is synthetic. See docs/dj-eval-demo.md.

Usage: PYTHONPATH=. python3 scripts/dev/demo_eval_dj.py
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts import eval_dj_v2  # noqa: E402

SEED = 7
N_ARTISTS = 18
N_TRACKS = 60
N_EVENTS = 400
SLATE_K = 10
EVAL_K = 5


def build_db(path: str) -> None:
    rng = random.Random(SEED)
    artists = [f"Artist {i:02d}" for i in range(N_ARTISTS)]
    catalog = [
        (f"t{i:03d}", f"Track {i:03d}", artists[i % N_ARTISTS])
        for i in range(N_TRACKS)
    ]
    # long-tail popularity: a few tracks dominate, most rarely surface
    weights = [1.0 / (i + 1) ** 0.7 for i in range(N_TRACKS)]

    with sqlite3.connect(path) as conn:
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
            catalog,
        )
        for e in range(N_EVENTS):
            slate_idx = rng.sample(range(N_TRACKS), k=SLATE_K, counts=None)
            slate_idx.sort(key=lambda i: weights[i] + rng.uniform(0, 0.35), reverse=True)
            ranked = []
            for i in slate_idx:
                tid, title, artist = catalog[i]
                novelty = round(1.0 - weights[i] / weights[0], 3)
                ranked.append(
                    {
                        "track_id": tid,
                        "title": title,
                        "artist": artist,
                        "rank_features": {"novelty": novelty},
                    }
                )
            # position-biased choice: earlier slots get picked more often
            pos = min(int(rng.expovariate(0.55)), SLATE_K - 1)
            chosen = ranked[pos]["track_id"]
            # skips grow more likely further down the long tail
            p_skip = 0.15 + 0.5 * ranked[pos]["rank_features"]["novelty"]
            if rng.random() < p_skip:
                outcome = {
                    "events": [
                        {"event_type": "skip", "skip_severity": round(rng.uniform(0.3, 0.95), 2)}
                    ]
                }
            else:
                outcome = {"events": [{"event_type": "complete"}]}
            conn.execute(
                "INSERT INTO dj_recs_audit_v2(event_id, ranked_json, chosen_track_id, outcome_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (f"evt{e:04d}", json.dumps(ranked), chosen, json.dumps(outcome), 1_700_000_000 + e),
            )
        conn.commit()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "synthetic_audit.db")
        build_db(db)
        metrics = eval_dj_v2.evaluate(db, k=EVAL_K)
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
