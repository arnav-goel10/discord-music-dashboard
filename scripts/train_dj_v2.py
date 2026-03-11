#!/usr/bin/env python3
"""Train lightweight ranker weights from AI DJ v2 logs.

This script is intentionally dependency-light so it can run in the bot repo
without extra ML infrastructure.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
from collections import defaultdict
from typing import Any


DEFAULT_WEIGHTS = {
    "completion_prob": 0.36,
    "skip_risk": -0.34,
    "novelty": 0.10,
    "session_fit": 0.31,
    "artist_affinity": 0.12,
    "semantic_similarity": 0.17,
    "sequential_affinity": 0.15,
    "co_listen_strength": 0.13,
    "rule_affinity": 0.09,
    "recency_penalty": -0.18,
    "artist_repetition_penalty": -0.14,
    "resolver_confidence": 0.08,
    "retrieval_score": 0.22,
}


def _db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _safe_json(raw: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


def fit_weights(db_path: str) -> dict[str, float]:
    gradients = defaultdict(float)
    counts = defaultdict(int)

    with _db_connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT ranked_json, outcome_json
            FROM dj_recs_audit_v2
            WHERE served_model LIKE 'v2%'
            ORDER BY created_at DESC
            LIMIT 20000
            """
        ).fetchall()

    if not rows:
        return dict(DEFAULT_WEIGHTS)

    for row in rows:
        ranked = _safe_json(row["ranked_json"], [])
        outcome = _safe_json(row["outcome_json"], {})
        events = outcome.get("events") if isinstance(outcome, dict) else []
        if not isinstance(ranked, list) or not ranked:
            continue

        was_skip = False
        for ev in events if isinstance(events, list) else []:
            if isinstance(ev, dict) and str(ev.get("event_type")) == "skip":
                was_skip = True
                break

        # Treat non-skip as positive reward proxy.
        target = 0.0 if was_skip else 1.0

        # Only use top recommendation for online reward fitting.
        top = ranked[0] if isinstance(ranked[0], dict) else {}
        feats = top.get("rank_features") if isinstance(top.get("rank_features"), dict) else {}
        for key, base in DEFAULT_WEIGHTS.items():
            val = float(feats.get(key, 0.0) or 0.0)
            # Centered gradient proxy.
            pred = 1.0 / (1.0 + math.exp(-val))
            gradients[key] += (target - pred) * val
            counts[key] += 1

    weights = dict(DEFAULT_WEIGHTS)
    for key, base in DEFAULT_WEIGHTS.items():
        n = max(1, counts[key])
        step = gradients[key] / n
        # Small bounded update to keep behavior stable.
        weights[key] = base + max(-0.08, min(0.08, 0.2 * step))

    return weights


def main() -> int:
    parser = argparse.ArgumentParser(description="Train AI DJ v2 ranker weights from audit logs.")
    parser.add_argument("--db", default=os.path.join("data", "music_bot.db"), help="Path to sqlite db")
    parser.add_argument(
        "--out",
        default=os.path.join("data", "dj_ranker_weights.json"),
        help="Output JSON path",
    )
    args = parser.parse_args()

    weights = fit_weights(args.db)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    payload = {
        "version": "dj_v2_ranker_weights_v1",
        "trained_at": int(time.time()),
        "weights": weights,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"Saved weights to {args.out}")
    for key in sorted(weights):
        print(f"  {key}: {weights[key]:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
