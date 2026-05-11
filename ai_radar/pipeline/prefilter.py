"""Prefilter step: classify items as AI-related (1) or not (0) via kimi-cli.

Pulls items where ``is_ai_related IS NULL``, batches them, sends a JSON-array
prompt through the Kimi backend, parses the model's JSON-array reply, and
writes the verdicts back.

Failure modes (all leave the row at NULL for next-run retry):
- CLI timeout / non-zero exit
- JSON unparseable
- IDs in reply not matching request

Iron Law A: this is the cheap step (kimi-cli is local, free); never escalate
to a paid API.

Input shape (2026-05-07 redesign): per item we send title + ``intro+outro``
extract of the summary — first 1-2 paragraphs (~300 chars) + last paragraph
(~150 chars), connected by ``[…]``. Skipping the middle keeps signal high
without forcing the model to read 600 chars of filler. Works because the
core "what is this about" lives in the lede; the close usually summarises.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ai_radar import db
from ai_radar.pipeline._batch_llm import BatchedLLMStep
from ai_radar.pipeline._llm import KimiCLIBackend, LLMBackend

BATCH_SIZE = 40
INTRO_CHARS = 300
OUTRO_CHARS = 150
PROMPT_FILE = "prompts/prefilter.md"


_P_TAG = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_PARA_SPLIT = re.compile(r"\n\s*\n")
_WS = re.compile(r"\s+")


def _intro_outro(
    text: str, *, intro_chars: int = INTRO_CHARS, outro_chars: int = OUTRO_CHARS,
) -> str:
    """Pick lead paragraphs + last paragraph from a summary blob.

    Strategy:
    1. Try splitting on ``<p>`` tags (RSS feeds preserve them).
    2. Fall back to double-newline splits.
    3. Whitespace-normalise each paragraph.
    4. If the whole text fits in ``intro+outro`` budget, return it whole.
    5. Otherwise build "intro paragraphs (up to intro_chars) + […] + last".

    Always returns at least the lede; never returns more than ~intro+outro chars.
    """
    if not text:
        return ""

    paras = [_HTML_TAG.sub("", p).strip() for p in _P_TAG.findall(text)]
    if not paras:
        cleaned = _HTML_TAG.sub("", text)
        paras = [p.strip() for p in _PARA_SPLIT.split(cleaned)]
    paras = [_WS.sub(" ", p) for p in paras if p.strip()]

    if not paras:
        return ""

    total = sum(len(p) for p in paras)
    if total <= intro_chars + outro_chars + 20 or len(paras) == 1:
        joined = "\n\n".join(paras)
        return joined if len(joined) <= intro_chars + outro_chars else joined[: intro_chars + outro_chars] + "…"

    intro_parts: list[str] = []
    used = 0
    for p in paras[:-1]:
        if used + len(p) <= intro_chars:
            intro_parts.append(p)
            used += len(p)
            continue
        remaining = intro_chars - used
        if remaining > 60:
            intro_parts.append(p[:remaining].rstrip() + "…")
        break

    last = paras[-1]
    if len(last) > outro_chars:
        last = last[:outro_chars].rstrip() + "…"

    return "\n\n".join(intro_parts + ["[…]", last])


@dataclass
class PendingItem:
    id: int
    title: str
    summary: str
    source: str


class Prefilter(BatchedLLMStep):
    name = "prefilter"

    def __init__(
        self,
        project_root: Path,
        *,
        batch_size: int = BATCH_SIZE,
        backend: LLMBackend | None = None,
    ) -> None:
        # Prefilter benchmark (2026-05-07, 4KB prompt + ~130 char output):
        #   KimiCLIBackend (subprocess + --max-steps 1):  avg 5.6s ✅
        #   KimiAPIBackend (HTTP stream):                 avg 17.1s
        # Subprocess wins because the HTTP path has to traverse the laptop
        # reverse tunnel (mihomo:65530) before exiting; kimi-cli uses its own
        # OAuth-backed transport with connection pooling. For small prompts
        # the subprocess startup cost is negligible. Keep prefilter on CLI.
        super().__init__(
            project_root,
            batch_size=batch_size,
            backend=backend or KimiCLIBackend(cwd=str(project_root)),
            prompt_file=PROMPT_FILE,
        )

    def _prompt_section_header(self) -> str:
        return "待判断条目"

    def _load_pending(
        self, conn: sqlite3.Connection, *, limit: int | None
    ) -> list[PendingItem]:
        """Read-only preview — drives the progress-bar total. Claim happens in _claim_batch."""
        sql = (
            "SELECT i.id, i.title, COALESCE(i.summary, '') AS summary, "
            "       s.name AS source "
            "FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE i.is_ai_related IS NULL "
            "ORDER BY i.fetched_at DESC"
        )
        params: list = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        cur = conn.execute(sql, params)
        return [self._row_to_item(r) for r in cur.fetchall()]

    def _claim_batch(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str,
        limit: int,
        stale_minutes: int,
    ) -> list[PendingItem]:
        rows = db.claim_pending_for_prefilter(
            conn, owner=owner, limit=limit, stale_minutes=stale_minutes,
        )
        return [self._row_to_item(r) for r in rows]

    def _unfinished_ids(
        self, conn: sqlite3.Connection, claimed_ids: list[int],
    ) -> list[int]:
        return db.items_still_pending_prefilter(conn, item_ids=claimed_ids)

    @staticmethod
    def _row_to_item(r) -> "PendingItem":
        return PendingItem(
            id=r["id"],
            title=(r["title"] or "").strip(),
            summary=_intro_outro(r["summary"] or ""),
            source=r["source"],
        )

    def _build_prompt_items(self, batch: list[PendingItem]) -> list[dict]:
        return [
            {"id": it.id, "source": it.source, "title": it.title, "summary": it.summary}
            for it in batch
        ]

    def _parse_and_write(
        self,
        conn: sqlite3.Connection,
        payload: list,
        requested_ids: set[int],
    ) -> int:
        written = 0
        for v in payload:
            if not isinstance(v, dict):
                continue
            vid = v.get("id")
            ai = v.get("ai")
            if vid in requested_ids and isinstance(ai, bool):
                conn.execute(
                    "UPDATE items SET is_ai_related = ? WHERE id = ?",
                    (1 if ai else 0, vid),
                )
                written += 1
        return written

    def _mark_filtered(self, conn: sqlite3.Connection, item_id: int) -> None:
        db.mark_item_excluded(conn, item_id=item_id)

    def _build_stats(self, pending: list[PendingItem]) -> dict:
        return {
            "step": self.name,
            "pending": len(pending),
            "ai_yes": 0,
            "ai_no": 0,
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
        ids = [it.id for it in pending]
        if not ids:
            return stats
        placeholder = ",".join("?" * len(ids))
        cur = conn.execute(
            f"SELECT is_ai_related, COUNT(*) AS n FROM items "
            f"WHERE id IN ({placeholder}) GROUP BY is_ai_related",
            ids,
        )
        for row in cur.fetchall():
            if row["is_ai_related"] == 1:
                stats["ai_yes"] = row["n"]
            elif row["is_ai_related"] == 0:
                stats["ai_no"] = row["n"]
        return stats
