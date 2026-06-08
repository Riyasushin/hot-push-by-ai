"""RSS / Atom fetcher.

Architecture: ``httpx`` does the network call (with **explicit timeout**),
``feedparser`` parses the bytes. Doing it this way means a hung server can't
freeze the whole fetch for 12+ minutes — feedparser by itself uses ``urllib``
without a default timeout.

Honours HTTP conditional GET via ETag / Last-Modified to avoid re-fetching
unchanged feeds. URL normalisation strips common tracking parameters before
items reach the DB so dedup stays clean even when sources tack on UTM noise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import struct_time
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from ai_radar.fetchers.base import FetchResult, Item

if TYPE_CHECKING:
    from ai_radar.db import SourceRow

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "ref_url", "mc_cid", "mc_eid", "fbclid", "gclid",
}

_USER_AGENT = "ai-radar/0.1 (+https://github.com/local; personal aggregator)"

# Hard cap to keep one slow source from blocking the whole cron run.
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _is_loopback_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower()
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _normalize_url(url: str) -> str:
    if not url:
        return url
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if k.lower() not in _TRACKING_PARAMS]
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc,
        parts.path,
        urlencode(query_pairs),
        "",  # drop fragment
    ))


def _parse_time(t: struct_time | None) -> datetime | None:
    if t is None:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class RSSFetcher:
    name = "rss"

    def fetch(self, source: "SourceRow") -> FetchResult:  # noqa: D401
        if not source.url:
            return FetchResult(items=[], error="empty url",
                               etag=source.etag, last_modified=source.last_modified)

        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, "
                      "application/xml;q=0.9, */*;q=0.8",
        }
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified

        try:
            with httpx.Client(
                timeout=_TIMEOUT,
                follow_redirects=True,
                headers=headers,
                trust_env=not _is_loopback_url(source.url),
            ) as client:
                resp = client.get(source.url)
        except httpx.TimeoutException as exc:
            return FetchResult(
                items=[], error=f"timeout: {exc!r}",
                etag=source.etag, last_modified=source.last_modified,
            )
        except httpx.HTTPError as exc:
            return FetchResult(
                items=[], error=f"http: {exc!r}",
                etag=source.etag, last_modified=source.last_modified,
            )

        if resp.status_code == 304:
            return FetchResult(
                items=[], not_modified=True,
                etag=source.etag, last_modified=source.last_modified,
            )
        if resp.status_code >= 400:
            return FetchResult(
                items=[], error=f"HTTP {resp.status_code}",
                etag=source.etag, last_modified=source.last_modified,
            )

        feed = feedparser.parse(resp.content)

        bozo_err = None
        if feed.bozo:
            # Surface bozo even when there are entries — partial parses are
            # the common silent-data-loss case (Atom feeds with one bad entry,
            # encoding mismatches, etc.). Caller writes this to fetch_runs.errors.
            bozo_err = repr(getattr(feed, "bozo_exception", "bozo"))

        items: list[Item] = []
        for entry in feed.entries:
            link = _normalize_url(entry.get("link") or entry.get("id") or "")
            if not link:
                continue
            title = (entry.get("title") or "").strip() or link
            summary = entry.get("summary") or entry.get("description")
            author = entry.get("author")
            content_list = entry.get("content") or []
            raw_content = content_list[0].get("value") if content_list else None

            published = _parse_time(entry.get("published_parsed")) \
                or _parse_time(entry.get("updated_parsed"))

            items.append(Item(
                url=link,
                title=title,
                summary=summary,
                raw_content=raw_content,
                author=author,
                published_at=published,
            ))

        new_etag = resp.headers.get("etag") or source.etag
        new_last_mod = resp.headers.get("last-modified") or source.last_modified

        return FetchResult(
            items=items,
            etag=new_etag,
            last_modified=new_last_mod,
            error=bozo_err,
        )
