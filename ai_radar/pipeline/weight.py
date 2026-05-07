"""Weight + select step.

Pure code, **no LLM**. Reads weights.toml, walks the scores table, computes:

    weighted_avg  = sum(dim_i * w_i) / sum(w_i)        # 0-10 scale
    total         = weighted_avg × tier_multiplier      # tier bonus / penalty
    is_selected   = 1 iff total ≥ thresholds[category][tier] else 0

Idempotent — safe to re-run any time after editing weights.toml.
Iron Law A in action: total / select are derived from per-item judgements,
recomputable instantly without re-spending the score-step model budget.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from ai_radar import config as cfg
from ai_radar import db


class Weighter:
    name = "weight"

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.config = cfg.load_config(project_root)

    def run(self, conn: sqlite3.Connection) -> dict:
        dims = tuple(self.config.weights.dims)
        weights = {d: self.config.weights.weights.get(d, 0.0) for d in dims}
        weight_sum = sum(weights.values())
        if weight_sum <= 0:
            return {"step": self.name, "error": "no positive weights for LLM dims"}

        tier_mul = self.config.weights.tier_multiplier  # already canonicalised T1.5
        thresholds = self.config.weights.thresholds      # {category: {tier: float}}

        per_category: dict[str, int] = defaultdict(int)
        per_category_selected: dict[str, int] = defaultdict(int)
        rows = db.all_scored_with_source(conn)
        # One transaction for the whole pass — keeps the writer lock for
        # one short burst instead of N implicit autocommits, and avoids
        # contention with the web server's feedback writes.
        with db.transaction(conn):
            for row in rows:
                weighted = sum(row[d] * weights[d] for d in dims)
                avg = weighted / weight_sum
                mul = tier_mul.get(row["source_tier"], 1.0)
                total = avg * mul

                cat = row["category"]
                tier = row["source_tier"]
                cat_thresholds = thresholds.get(cat, {})
                thresh = cat_thresholds.get(tier)
                is_selected = 1 if (thresh is not None and total >= thresh) else 0

                db.update_total_and_selection(
                    conn, item_id=row["item_id"], total=total, is_selected=is_selected,
                )
                per_category[cat] += 1
                if is_selected:
                    per_category_selected[cat] += 1

        return {
            "step": self.name,
            "scored_total": len(rows),
            "selected_total": sum(per_category_selected.values()),
            "per_category": [
                (c, per_category_selected[c], per_category[c])
                for c in per_category
            ],
            "weights": weights,
            "weight_sum": weight_sum,
            "tier_multiplier": dict(tier_mul),
            "thresholds": dict(thresholds),
        }
