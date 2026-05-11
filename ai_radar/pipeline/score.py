"""Score step: N-dim scoring + category + Chinese summary + 推荐理由.

Selects items with ``is_ai_related = 1`` not yet present in the ``scores`` table,
batches them, sends to the chosen LLM backend, parses the JSON-array reply,
and ``upsert_score``s each entry.

Iron Law A: this is the **expensive** step — uses a world-knowledge-strong
model (default ``DeepSeekBackend``). Don't downgrade to a small model to save
money. Pass ``--backend kimi`` only if you accept the world-knowledge tradeoff.

The score dimensions are read from ``weights.toml [scoring].dims``; this
module is dim-agnostic. Adding a 5th dim = toml edit + ``ALTER TABLE scores
ADD COLUMN`` + prompt update.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ai_radar import config as cfg
from ai_radar import db
from ai_radar.pipeline._batch_llm import BatchedLLMStep
from ai_radar.pipeline._llm import DeepSeekBackend, LLMBackend
from ai_radar.pipeline.prefilter import _intro_outro

BATCH_SIZE = 5  # smaller than prefilter — output payload is much richer
PROMPT_FILE = "prompts/score.md"
# Score input budget. Score gets more text than prefilter (1200 vs 450 chars)
# because category + summary_zh + reason all need substance, not just AI/non-AI.
# Intro+outro pattern (head + tail with [...] gap) preserves both lede and
# takeaways — critical for long Zhihu answers / 公众号 long-form, where the
# meaty conclusion often sits at the end.
SCORE_INTRO_CHARS = 800
SCORE_OUTRO_CHARS = 400


@dataclass
class PendingItem:
    id: int
    title: str
    summary: str
    source_name: str
    source_tier: str
    source_category: str


class Scorer(BatchedLLMStep):
    name = "score"

    def __init__(
        self,
        project_root: Path,
        *,
        batch_size: int = BATCH_SIZE,
        backend: LLMBackend | None = None,
        config: cfg.Config | None = None,
    ) -> None:
        super().__init__(
            project_root,
            batch_size=batch_size,
            backend=backend or DeepSeekBackend(),
            prompt_file=PROMPT_FILE,
        )
        self._config = config or cfg.load_config(project_root)
        self._dims: tuple[str, ...] = tuple(self._config.weights.dims)
        self._allowed_categories: frozenset[str] = frozenset(
            self._config.weights.categories
            or ["论文研究", "infra工程", "模型发布", "产品发布", "行业经济", "技巧与观点"]
        )

    def _prompt_section_header(self) -> str:
        return "待评分条目"

    def _load_pending(
        self, conn: sqlite3.Connection, *, limit: int | None
    ) -> list[PendingItem]:
        """Read-only preview — drives the progress-bar total. Claim happens in _claim_batch."""
        rows = db.pending_for_scoring(conn, limit=limit)
        return [self._row_to_item(r) for r in rows]

    def _claim_batch(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str,
        limit: int,
        stale_minutes: int,
    ) -> list[PendingItem]:
        rows = db.claim_pending_for_scoring(
            conn, owner=owner, limit=limit, stale_minutes=stale_minutes,
        )
        return [self._row_to_item(r) for r in rows]

    def _unfinished_ids(
        self, conn: sqlite3.Connection, claimed_ids: list[int],
    ) -> list[int]:
        return db.items_still_pending_scoring(conn, item_ids=claimed_ids)

    @staticmethod
    def _row_to_item(r) -> "PendingItem":
        return PendingItem(
            id=r["id"],
            title=(r["title"] or "").strip(),
            summary=_intro_outro(
                r["summary"] or "",
                intro_chars=SCORE_INTRO_CHARS,
                outro_chars=SCORE_OUTRO_CHARS,
            ),
            source_name=r["source_name"],
            source_tier=r["source_tier"],
            source_category=r["source_category"],
        )

    def _build_prompt_items(self, batch: list[PendingItem]) -> list[dict]:
        return [
            {
                "id": it.id,
                "source": it.source_name,
                "source_tier": it.source_tier,
                "title": it.title,
                "summary": it.summary,
            }
            for it in batch
        ]

    def _parse_and_write(
        self,
        conn: sqlite3.Connection,
        payload: list,
        requested_ids: set[int],
    ) -> int:
        written = 0
        for entry in payload:
            try:
                self._write_one(conn, entry, requested_ids)
                written += 1
            except _SkipEntry as e:
                # Single entry malformed; rest of the batch still persisted.
                # No log spam — caller already counts batches_ok / batches_failed.
                _ = e
        return written

    def _write_one(
        self,
        conn: sqlite3.Connection,
        entry: object,
        requested_ids: set[int],
    ) -> None:
        if not isinstance(entry, dict):
            raise _SkipEntry("not a dict")
        item_id = entry.get("id")
        if item_id not in requested_ids:
            raise _SkipEntry(f"unknown id {item_id!r}")

        scores = entry.get("scores")
        if not isinstance(scores, dict):
            raise _SkipEntry("scores missing or not an object")
        try:
            dim_vals = {d: _coerce_score(scores[d]) for d in self._dims}
        except (KeyError, ValueError, TypeError) as e:
            raise _SkipEntry(f"bad scores: {e}")

        category = entry.get("category")
        if category not in self._allowed_categories:
            raise _SkipEntry(f"bad category {category!r}")

        summary_zh = entry.get("summary_zh") or None
        reason = entry.get("reason") or None

        db.upsert_score(
            conn,
            item_id=item_id,
            hardcore=dim_vals.get("hardcore", 0.0),
            primary_src=dim_vals.get("primary_src", 0.0),
            density=dim_vals.get("density", 0.0),
            novelty=dim_vals.get("novelty", 0.0),
            category=category,
            summary_zh=summary_zh,
            reason=reason,
            model=self.backend.name,
        )

    def _mark_filtered(self, conn: sqlite3.Connection, item_id: int) -> None:
        # Score-side bisect: Kimi rejected this item's prompt as high-risk.
        # Same sentinel as prefilter — flip to is_ai_related=-1 so the score
        # claim SQL (which selects is_ai_related=1) won't pick it up again.
        db.mark_item_excluded(conn, item_id=item_id)

    def _build_stats(self, pending: list[PendingItem]) -> dict:
        return {
            "step": self.name,
            "model": getattr(self.backend, "name", "?"),
            "pending": len(pending),
            "scored": 0,
            "batches_ok": 0,
            "batches_failed": 0,
            "errors": [],
            "written": 0,
        }

    def _finalize_stats(
        self,
        conn: sqlite3.Connection,
        stats: dict,
        pending: list[PendingItem],
    ) -> dict:
        # Mirror "written" into the historical "scored" key so CLI + report
        # don't have to know about the new generic key.
        stats["scored"] = stats.get("written", 0)
        return stats


class _SkipEntry(Exception):
    """Raised when a single entry in the batch reply is malformed."""


def _coerce_score(v: object) -> float:
    if isinstance(v, bool):  # bool is subclass of int — reject explicitly
        raise ValueError("bool not allowed")
    if not isinstance(v, (int, float)):
        raise ValueError(f"not numeric: {v!r}")
    f = float(v)
    if not (0.0 <= f <= 10.0):
        raise ValueError(f"out of range: {f}")
    return f
