"""Shared scaffolding for LLM-driven batch pipeline steps.

Both ``Prefilter`` and ``Scorer`` follow the same shape:

    pending = load rows that need work
    for batch in chunks(pending):
        prompt    = system_prompt + JSON-encoded batch
        raw_reply = llm.complete(prompt)
        verdicts  = parse JSON
        write each verdict back to DB

This module pulls that shape into ``BatchedLLMStep``. Subclasses override the
small parts that differ:

- ``_load_pending``        — what rows to pick up
- ``_build_prompt_items``  — what fields to send the model per row
- ``_parse_and_write``     — how to validate + persist each verdict
- ``_build_stats`` / ``_finalize_stats`` — per-step counters

Iron Law A still applies: subclasses pick their own LLMBackend (kimi vs
DeepSeek) — this module doesn't choose.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ai_radar import db
from ai_radar.pipeline._llm import KimiContentFilterError, LLMBackend, extract_json

log = logging.getLogger(__name__)

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class BatchOutcome:
    requested: int
    written: int
    failed: bool = False
    error: str | None = None


class BatchedLLMStep(ABC):
    name: str

    def __init__(
        self,
        project_root: Path,
        *,
        batch_size: int,
        backend: LLMBackend,
        prompt_file: str,
    ) -> None:
        self.project_root = project_root
        self.batch_size = batch_size
        self.backend = backend
        self.prompt_file = prompt_file
        self._system_prompt: str | None = None

    def run(
        self,
        conn: sqlite3.Connection,
        *,
        limit: int | None = None,
        on_batch=None,  # callable(batch_index, total_batches, outcome, batch) -> None
        stale_minutes: int = 60,
    ) -> dict:
        # Multi-consumer dedup: rather than SELECTing the whole pending set up
        # front (two concurrent runs would race on the same ids), we claim a
        # batch at a time via UPDATE...RETURNING — see ai_radar.db helpers.
        owner = _owner_uuid()
        db.release_stale_claims(conn, max_age_minutes=stale_minutes)

        # `total` for the progress bar is unknown ahead of time when we claim
        # incrementally; use the up-front pending count as an upper bound, and
        # let the callback advance batch-by-batch. _load_pending stays around
        # for this and for stats reporting; it does NOT drive batch dispatch.
        pending_preview = self._load_pending(conn, limit=limit)
        stats = self._build_stats(pending_preview)
        if not pending_preview:
            return self._finalize_stats(conn, stats, pending_preview)

        approx_total_batches = max(1, (len(pending_preview) + self.batch_size - 1) // self.batch_size)

        claimed_ids: list[int] = []
        processed_batches: list[list] = []
        remaining = limit
        i = 0
        try:
            while remaining is None or remaining > 0:
                want = self.batch_size
                if remaining is not None:
                    want = min(want, remaining)
                batch = self._claim_batch(
                    conn, owner=owner, limit=want, stale_minutes=stale_minutes,
                )
                if not batch:
                    break
                i += 1
                claimed_ids.extend(it.id for it in batch)
                processed_batches.append(batch)

                outcome = self._process_batch(conn, batch)
                if outcome.failed:
                    stats["batches_failed"] += 1
                    stats["errors"].append(outcome.error)
                else:
                    stats["batches_ok"] += 1
                stats.setdefault("written", 0)
                stats["written"] += outcome.written
                if on_batch is not None:
                    try:
                        on_batch(i, max(approx_total_batches, i), outcome, batch)
                    except Exception:
                        # Callback errors must never break the pipeline.
                        pass

                if remaining is not None:
                    remaining -= len(batch)
        finally:
            unfinished = self._unfinished_ids(conn, claimed_ids)
            if unfinished:
                db.release_claim(conn, item_ids=unfinished, owner=owner)

        # Hand finalize_stats the items we actually saw, not the up-front
        # preview (which can include rows another worker grabbed first).
        seen = [it for batch in processed_batches for it in batch]
        return self._finalize_stats(conn, stats, seen or pending_preview)

    def _process_batch(
        self, conn: sqlite3.Connection, batch: list
    ) -> BatchOutcome:
        prompt = self._build_prompt(batch)
        try:
            raw = self.backend.complete(prompt)
        except KimiContentFilterError:
            # One (or more) item in this batch trips Kimi's content moderation.
            # Bisect to isolate, then mark the offender via _mark_filtered so
            # it leaves the pending pool permanently — otherwise next run
            # re-claims it and trips the same filter again.
            return self._bisect_on_content_filter(conn, batch)
        except Exception as exc:  # noqa: BLE001 — backend can fail many ways
            return BatchOutcome(
                requested=len(batch), written=0, failed=True, error=repr(exc)
            )

        try:
            payload = extract_json(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: JSON parse failed: %s\nraw=%r", self.name, exc, raw[:500])
            return BatchOutcome(
                requested=len(batch), written=0, failed=True, error=f"parse: {exc}"
            )

        if not isinstance(payload, list):
            return BatchOutcome(
                requested=len(batch), written=0, failed=True,
                error="reply was not a JSON array",
            )

        requested_ids = {it.id for it in batch}
        written = self._parse_and_write(conn, payload, requested_ids)
        return BatchOutcome(requested=len(batch), written=written)

    def _bisect_on_content_filter(
        self, conn: sqlite3.Connection, batch: list,
    ) -> BatchOutcome:
        if len(batch) == 1:
            it = batch[0]
            self._mark_filtered(conn, it.id)
            log.warning(
                "%s: content_filter rejected single item id=%s, marked excluded "
                "(is_ai_related=-1).", self.name, it.id,
            )
            # Counted as 'requested' but not 'written' — caller stats will
            # show pending-not-scored, while the row itself is out of the pool.
            return BatchOutcome(requested=1, written=0)
        mid = len(batch) // 2
        left = self._process_batch(conn, batch[:mid])
        right = self._process_batch(conn, batch[mid:])
        return BatchOutcome(
            requested=left.requested + right.requested,
            written=left.written + right.written,
            failed=left.failed or right.failed,
            error=left.error or right.error,
        )

    def _build_prompt(self, batch: list) -> str:
        if self._system_prompt is None:
            self._system_prompt = (
                self.project_root / self.prompt_file
            ).read_text(encoding="utf-8")
        items = self._build_prompt_items(batch)
        header = self._prompt_section_header()
        return (
            f"{self._system_prompt}\n\n"
            f"## {header}\n\n"
            f"```json\n{json.dumps(items, ensure_ascii=False)}\n```\n"
        )

    def _prompt_section_header(self) -> str:
        return "待处理条目"

    @staticmethod
    def _truncate(s: str, n: int) -> str:
        if not s:
            return ""
        s = _HTML_TAG.sub("", s)
        s = _WHITESPACE.sub(" ", s).strip()
        return s if len(s) <= n else s[:n] + "…"

    # --- subclass hooks ---

    @abstractmethod
    def _load_pending(self, conn: sqlite3.Connection, *, limit: int | None) -> list:
        """Read-only preview of pending items — used for stats / progress total.

        The actual batch dispatch goes through ``_claim_batch`` so that two
        concurrent workers don't race on the same rows.
        """

    @abstractmethod
    def _claim_batch(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str,
        limit: int,
        stale_minutes: int,
    ) -> list:
        """Atomically claim and return up to ``limit`` pending items for ``owner``.

        Implementations call the matching ``ai_radar.db.claim_pending_for_*``
        helper. Returns ``[]`` when nothing's left to claim.
        """

    @abstractmethod
    def _unfinished_ids(
        self, conn: sqlite3.Connection, claimed_ids: list[int],
    ) -> list[int]:
        """Subset of ``claimed_ids`` whose business column is still unwritten.

        Called from ``run()``'s ``finally`` to release claims we didn't manage
        to process — so a sibling worker (or next run) can pick them up
        immediately, without waiting on the stale-claim sweep.
        """

    @abstractmethod
    def _build_prompt_items(self, batch: list) -> list[dict]:
        ...

    @abstractmethod
    def _parse_and_write(
        self, conn: sqlite3.Connection, payload: list, requested_ids: set[int]
    ) -> int:
        """Write verdicts. Returns count of items actually persisted."""

    @abstractmethod
    def _mark_filtered(self, conn: sqlite3.Connection, item_id: int) -> None:
        """Permanently exclude one item from this step's pending pool.

        Called when bisect has narrowed a Kimi content_filter rejection down
        to a single item — that item must leave the pool, otherwise the next
        run re-claims it and trips the same filter. Implementations typically
        flip a sentinel column (we use ``is_ai_related = -1``).
        """

    @abstractmethod
    def _build_stats(self, pending: list) -> dict:
        ...

    def _finalize_stats(
        self, conn: sqlite3.Connection, stats: dict, pending: list
    ) -> dict:
        return stats


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _owner_uuid() -> str:
    """Per-process claim owner — host/pid/short-uuid for grep-friendly debug.

    Example: ``laptop/40213/9c1ea2b7``. Lets you eyeball which terminal grabbed
    a row when poking at the DB.
    """
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:8]}"
