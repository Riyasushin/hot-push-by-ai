"""WeChat Reading (微信读书) fetcher.

**2026-05-07 — capability upgrade**: previously thought to be discovery-only
(via ``chapterInfos``, which still returns ``updated:[]`` for MP books).
We discovered the SPA reader at ``/web/mp/reader/`` is backed by a different
endpoint, ``GET /web/mp/articles?bookId=<MP_WXS_*>``, which **does** return
the per-公众号 article list with title / preview-content / publish time /
the original ``mp.weixin.qq.com`` short id (``originalId``).

**Item URL** is ``https://mp.weixin.qq.com/s/<originalId>``. Verified the
canonical 公众号 URL returns 200 directly (no captcha) when called with a
modern browser User-Agent + a Referer like ``https://weread.qq.com/``. The
Sogou middleman (``weixin.sogou.com``) is NOT needed — it would in fact
fail because Sogou's ``/link?...`` redirector triggers anti-spider.

What we still don't have:
    - Full article body (mp.weixin response has it; we only store the WeRead
      preview ``content`` ~120 chars, enough for prefilter + score).
    - Mobile-only ``i.weread.qq.com`` endpoints (different ``accessToken`` auth).

**Cookie session is SHORT** — WeRead web ``wr_skey`` invalidates within hours.
Look for ``weread-auth:`` errors in ``fetch_runs.errors`` and re-grab cookie
from DevTools when seen. For cron-friendly long-term use, prefer self-hosted
WeWe RSS (path B in ``docs/ADDING_SOURCES.md``).

Endpoints used:
    GET  /web/shelf/sync                — full shelf, incl. mp section
    GET  /web/mp/articles?bookId=...    — ⭐ article list for one 公众号
    GET  /web/mp/cover?bookId=...       — latest single article + cover info
    POST /web/book/chapterInfos         — normal books only (kept for non-MP)

Source URL convention:
    weread://shelf                  → walk shelf, fetch articles for every MP_ book
    weread://book/<bookId>          → specific bookId; auto-detects MP vs normal book
    weread://mpbook                 → legacy lumped 文章收藏 (rarely useful)

Run ``radar weread-list`` to discover what bookIds you have on your shelf.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from ai_radar.fetchers.base import FetchResult, Item

if TYPE_CHECKING:
    from ai_radar.db import SourceRow


# Distinct error categories so the caller can tell what went wrong.
class WeReadCookieError(RuntimeError):
    """Cookie missing, expired, or 401. User needs to re-grab cookie."""


class WeReadEndpointError(RuntimeError):
    """API endpoint returned 404 / -2003. Code bug or API moved."""


class WeReadResponseError(RuntimeError):
    """Got a 200 but body is malformed / unparseable. Code or transient bug."""

BASE = "https://weread.qq.com"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


def _read_cookie() -> str:
    # Be lenient about case — shell convention is upper, but users often type
    # lowercase in .env. Check both.
    for var in ("WEREAD_COOKIE", "weread_cookie"):
        cookie = os.environ.get(var, "").strip()
        if cookie:
            # Sanity: must include wr_vid + wr_skey (HTTP-only auth tokens).
            # If user grabbed via JS document.cookie, those will be missing.
            if "wr_vid=" not in cookie or "wr_skey=" not in cookie:
                raise WeReadCookieError(
                    "Cookie loaded but missing wr_vid / wr_skey (HTTP-only). "
                    "Don't use `copy(document.cookie)` — it can't see HttpOnly. "
                    "Grab from DevTools Network tab → any request → Cookie header."
                )
            return cookie
    raise WeReadCookieError(
        "WEREAD_COOKIE / weread_cookie not set in env or .env."
    )


def _classify_response(r: httpx.Response) -> None:
    """Raise the right exception class based on shape of WeRead's reply."""
    if r.status_code == 401:
        raise WeReadCookieError(f"HTTP 401: cookie expired (re-grab it)")
    if r.status_code == 404:
        raise WeReadEndpointError(f"HTTP 404: endpoint missing or wrong path: {r.url}")
    if r.status_code >= 400:
        raise WeReadEndpointError(f"HTTP {r.status_code}: {r.text[:200]}")
    # 200 but error inside body
    try:
        body = r.json()
    except Exception:
        return  # not JSON, caller handles
    if isinstance(body, dict):
        errcode = body.get("errcode") or body.get("errCode")
        if errcode in (-2012,):  # 登录超时
            raise WeReadCookieError(f"errcode={errcode}: {body.get('errmsg') or body.get('errMsg')}")
        if errcode in (-2003,):  # 参数格式错误
            raise WeReadEndpointError(f"errcode={errcode}: {body.get('errmsg') or body.get('errMsg')}")
        if errcode and errcode < 0:
            raise WeReadResponseError(f"errcode={errcode}: {body.get('errmsg') or body.get('errMsg')}")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE,
        headers={
            "Cookie": _read_cookie(),
            "User-Agent": _USER_AGENT,
            "Referer": f"{BASE}/",
            "Accept": "application/json",
        },
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


# ---------- low-level API wrappers ----------

def shelf_sync(client: httpx.Client) -> dict:
    """Full bookshelf: books + lectureBooks + mp (公众号)."""
    r = client.get("/web/shelf/sync")
    _classify_response(r)
    return r.json()


def notebook_list(client: httpx.Client) -> dict:
    """Books with notes; sometimes the path WeRead uses to track 公众号 too."""
    r = client.get("/api/user/notebook")
    _classify_response(r)
    return r.json()


def chapter_infos(client: httpx.Client, book_ids: list[str]) -> dict:
    """Chapters of one or more books. NOTE: returns empty ``updated`` for MP_*
    bookIds — for 公众号, use ``mp_articles()`` instead.
    """
    r = client.post("/web/book/chapterInfos", json={"bookIds": book_ids})
    _classify_response(r)
    return r.json()


def mp_articles(client: httpx.Client, book_id: str) -> dict:
    """Article list for one 公众号 (MP_WXS_*) book.

    Response shape: ``{"reviews": [{"createTime", "subCount", "subReviews": [
    {"reviewId", "createTime", "belongBookId", "mpInfo": {...}, ...}, ...]}, ...
    ], "synckey": ..., "clearAll": 0}``.

    Each ``subReviews[].mpInfo`` carries ``title`` / ``content`` (preview) /
    ``time`` (unix seconds) / ``mp_name`` / ``pic_url`` / ``readNum`` /
    ``likeNum`` / ``originalId`` (= 微信 mp.weixin.qq.com short-id).
    """
    r = client.get("/web/mp/articles", params={"bookId": book_id})
    _classify_response(r)
    return r.json()


# ---------- fetcher ----------

class WeReadFetcher:
    name = "weread"

    def fetch(self, source: "SourceRow") -> FetchResult:
        if not source.url:
            return FetchResult(items=[], error="empty url")

        # Parse source.url: weread://mpbook | weread://shelf | weread://book/<id>
        path = source.url.replace("weread://", "", 1).strip("/")

        try:
            with _client() as client:
                if path == "mpbook":
                    items = self._fetch_book_articles(client, "mpbook")
                elif path == "shelf":
                    items = self._fetch_all_subscribed_mp(client)
                elif path.startswith("book/"):
                    book_id = path[len("book/"):]
                    items = self._fetch_book_articles(client, book_id)
                else:
                    return FetchResult(
                        items=[],
                        error=f"weread-bad-url: {path!r} "
                              f"(use mpbook | shelf | book/<id>)",
                    )
        except WeReadCookieError as exc:
            # Distinct so cron logs / status make it obvious to re-grab cookie.
            return FetchResult(items=[], error=f"weread-auth: {exc}")
        except WeReadEndpointError as exc:
            return FetchResult(items=[], error=f"weread-endpoint: {exc}")
        except WeReadResponseError as exc:
            return FetchResult(items=[], error=f"weread-response: {exc}")
        except httpx.HTTPError as exc:
            return FetchResult(items=[], error=f"weread-http: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            return FetchResult(items=[], error=f"weread-other: {exc!r}")

        return FetchResult(items=items)

    def _fetch_book_articles(self, client: httpx.Client, book_id: str) -> list[Item]:
        # 公众号 books (MP_*) go through the MP article-list endpoint;
        # everything else through chapterInfos.
        if book_id.startswith("MP_"):
            return _mp_book_to_items(mp_articles(client, book_id), book_id)
        return _normal_book_to_items(chapter_infos(client, [book_id]), book_id)

    def _fetch_all_subscribed_mp(self, client: httpx.Client) -> list[Item]:
        """Walk shelf, fetch /web/mp/articles for every MP_* book."""
        shelf = shelf_sync(client)

        mp_book_ids: list[str] = []
        for b in shelf.get("books", []) or []:
            bid = b.get("bookId", "")
            # Verified 2026-05-07: 公众号 are type=3 with bookId starting MP_*.
            if bid.startswith("MP_") and b.get("type") == 3:
                mp_book_ids.append(bid)

        items: list[Item] = []
        for bid in mp_book_ids:
            try:
                items.extend(_mp_book_to_items(mp_articles(client, bid), bid))
            except (WeReadCookieError, WeReadEndpointError, WeReadResponseError):
                # Re-raise — auth / endpoint problems are not per-book recoverable.
                raise
            except httpx.HTTPError:
                # Per-book transient HTTP issue: skip this book, keep going.
                continue
        return items


# ---------- response → Item parsers ----------

def _mp_book_to_items(payload: dict, book_id: str) -> list[Item]:
    """Parse /web/mp/articles response into Item list.

    URL: ``https://mp.weixin.qq.com/s/<originalId>`` — the canonical 公众号
    article URL. Verified 2026-05-07 to return HTTP 200 in browser context
    (proper UA + Referer); no captcha for normal users. Sogou-search
    middleman not needed.
    """
    _ = book_id  # kept for caller symmetry; URL doesn't need it
    items: list[Item] = []
    for review in payload.get("reviews", []) or []:
        for sub in review.get("subReviews", []) or []:
            inner = sub.get("review", sub)
            mp = inner.get("mpInfo") or {}
            if not isinstance(mp, dict):
                continue
            title = (mp.get("title") or "").strip()
            if not title:
                continue
            original_id = mp.get("originalId") or ""
            if not original_id:
                # No stable id — skip rather than synthesize, otherwise dedup
                # collapses every idless article into one row.
                continue
            ts = mp.get("time") or inner.get("createTime") or 0
            published = (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                if ts and ts > 0 else None
            )
            items.append(Item(
                url=f"https://mp.weixin.qq.com/s/{original_id}",
                title=title,
                summary=(mp.get("content") or "").strip() or None,
                raw_content=None,
                author=mp.get("mp_name") or None,
                published_at=published,
            ))
    return items


def _normal_book_to_items(payload: dict, book_id: str) -> list[Item]:
    """Parse /web/book/chapterInfos response (regular ebooks, not 公众号)."""
    items: list[Item] = []
    for entry in payload.get("data", []) or []:
        if entry.get("bookId") != book_id:
            continue
        for ch in entry.get("updated", []) or []:
            ts = ch.get("updateTime") or 0
            published = (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                if ts and ts > 0 else None
            )
            title = (ch.get("title") or "").strip()
            if not title:
                continue
            url = f"{BASE}/web/reader/{book_id}_{ch['chapterUid']}"
            items.append(Item(
                url=url,
                title=title,
                summary=None,
                raw_content=None,
                author=None,
                published_at=published,
            ))
    return items
