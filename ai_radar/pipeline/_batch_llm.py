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
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ai_radar.pipeline._llm import LLMBackend, extract_json

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
    ) -> dict:
        pending = self._load_pending(conn, limit=limit)
        stats = self._build_stats(pending)
        if not pending:
            return stats

        batches = list(_chunks(pending, self.batch_size))
        total = len(batches)
        for i, batch in enumerate(batches, start=1):
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
                    on_batch(i, total, outcome, batch)
                except Exception:
                    # Callback errors must never break the pipeline.
                    pass

        return self._finalize_stats(conn, stats, pending)

    def _process_batch(
        self, conn: sqlite3.Connection, batch: list
    ) -> BatchOutcome:
        prompt = self._build_prompt(batch)
        try:
            raw = self.backend.complete(prompt)
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
        ...

    @abstractmethod
    def _build_prompt_items(self, batch: list) -> list[dict]:
        ...

    @abstractmethod
    def _parse_and_write(
        self, conn: sqlite3.Connection, payload: list, requested_ids: set[int]
    ) -> int:
        """Write verdicts. Returns count of items actually persisted."""

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
