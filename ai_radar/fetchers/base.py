"""Fetcher contract: every fetcher returns an iterable of Item.

Adding a new fetcher type only touches this directory plus the dispatcher
in ``ai_radar/fetchers/__init__.py``. The main fetch flow stays untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Protocol

if TYPE_CHECKING:
    from ai_radar.config import Source


@dataclass
class Item:
    url: str
    title: str
    summary: str | None = None
    raw_content: str | None = None
    author: str | None = None
    published_at: datetime | None = None


@dataclass
class FetchResult:
    """What a fetcher returns to the caller besides items, so it can update HTTP cache state."""

    items: list[Item] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: str | None = None


class Fetcher(Protocol):
    name: str

    def fetch(self, source: "Source") -> FetchResult: ...
