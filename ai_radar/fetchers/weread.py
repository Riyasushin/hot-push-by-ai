"""WeChat Reading (微信读书) fetcher.

Backed by ``GET /web/mp/articles?bookId=<MP_WXS_*>``, which returns the
per-公众号 article list (title / preview / publish time / mp.weixin short id).
Item URL is ``https://mp.weixin.qq.com/s/<originalId>`` — verified to return
200 directly with a modern browser UA + ``Referer: https://weread.qq.com/``;
the Sogou middleman is NOT needed (it triggers anti-spider).

What we don't have:
- Full article body. We store only the ~120 char WeRead preview, which is
  enough for prefilter + score.
- Mobile-only ``i.weread.qq.com`` endpoints (different ``accessToken`` auth).

Cookie maintenance: ``wr_skey`` has a 90-minute ABSOLUTE TTL; refreshed by
``scripts/weread-keepalive.sh`` via ``POST /web/login/renewal``. The 1-year
``wr_rt`` is the real long-lived credential. Errcode semantics + cookie-field
breakdown live in ``memory/reference_weread_cookie.md``.

Endpoints used:
    GET  /web/shelf/sync                — full shelf, incl. mp section
    GET  /web/mp/articles?bookId=...    — ⭐ article list for one 公众号
    POST /web/book/chapterInfos         — normal books (kept for non-MP)

Source URL convention:
    weread://shelf           → walk shelf, fetch articles for every MP_ book
    weread://book/<bookId>   → specific bookId (auto-detects MP vs normal)
    weread://mpbook          → legacy lumped 文章收藏 (rarely useful)

Run ``radar weread-list`` to discover bookIds on your shelf.
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from ai_radar.fetchers.base import FetchResult, Item

if TYPE_CHECKING:
    from ai_radar.db import SourceRow


BASE = "https://weread.qq.com"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
# Spread shelf-walk requests so we don't pulse-burst WeRead's mp.weixin proxy
# (which manifests as -10100 upstream timeouts when called too fast).
_PER_BOOK_JITTER_RANGE = (0.5, 1.2)
# One backoff retry on -10100 before giving up on this book this round.
_UPSTREAM_TIMEOUT_BACKOFF_S = 3.0


# ---------- exceptions ----------
#
# Errcode semantics are documented in ``memory/reference_weread_cookie.md``.
# Subclassing ``WeReadResponseError`` keeps callers that catch the parent
# class working unchanged while letting precise handlers branch on subclass.

class WeReadCookieError(RuntimeError):
    """Auth-side: cookie missing, HTTP 401, or errcode -2012. User re-grabs cookie."""


class WeReadEndpointError(RuntimeError):
    """Endpoint-side: HTTP 4xx / 404 / errcode -2003. Code bug or API moved."""


class WeReadResponseError(RuntimeError):
    """200-with-error-body. ``errcode`` carries the parsed negative code when
    known; ``None`` for malformed-body cases.
    """

    def __init__(self, msg: str, errcode: int | None = None) -> None:
        super().__init__(msg)
        self.errcode = errcode


class WeReadUpstreamTimeout(WeReadResponseError):
    """-10100: WeRead's own upstream call to mp.weixin timed out. Per-book
    transient — retry once with backoff, otherwise skip this book for the
    round (a sibling 公众号 likely still works).
    """


class WeReadBlockedError(WeReadResponseError):
    """Account-level dead-end: -2041 (need verify / 风控), -2050 (拉黑),
    -2054 (wr_rt 失效), -2063 (验证过期). Hammering only makes 风控 more
    aggressive — abort the walk and surface the error so the caller can
    re-login manually.
    """


# Errcode → exception class. Single source of truth for ``_classify_response``.
# Subclasses of WeReadResponseError get the (msg, errcode) constructor;
# the auth/endpoint classes take just msg.
_ERRCODE_TO_EXC: dict[int, type[Exception]] = {
    -2012: WeReadCookieError,
    -2003: WeReadEndpointError,
    -2041: WeReadBlockedError,
    -2050: WeReadBlockedError,
    -2054: WeReadBlockedError,
    -2063: WeReadBlockedError,
    -10100: WeReadUpstreamTimeout,
}


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
    """Raise the right exception class based on the shape of WeRead's reply."""
    if r.status_code == 401:
        raise WeReadCookieError("HTTP 401: cookie expired (re-grab it)")
    if r.status_code == 404:
        raise WeReadEndpointError(f"HTTP 404: endpoint missing or wrong path: {r.url}")
    if r.status_code >= 400:
        raise WeReadEndpointError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        body = r.json()
    except Exception:
        return  # not JSON; caller decides what to do
    if not isinstance(body, dict):
        return
    errcode = body.get("errcode") or body.get("errCode")
    if not errcode or errcode >= 0:
        return
    msg = f"errcode={errcode}: {body.get('errmsg') or body.get('errMsg')}"
    cls = _ERRCODE_TO_EXC.get(errcode, WeReadResponseError)
    if issubclass(cls, WeReadResponseError):
        raise cls(msg, errcode=errcode)
    raise cls(msg)


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


def mp_articles_with_retry(client: httpx.Client, book_id: str) -> dict:
    """``mp_articles`` with one backoff retry on -10100 upstream-timeout.

    Other errcodes (cookie / endpoint / 风控 / unknown) propagate unchanged —
    they're either auth-level (won't fix in 3s) or account-level (retrying
    only worsens 风控 per the errcode reference memory).
    """
    try:
        return mp_articles(client, book_id)
    except WeReadUpstreamTimeout:
        time.sleep(_UPSTREAM_TIMEOUT_BACKOFF_S)
        return mp_articles(client, book_id)


# ---------- fetcher ----------

class WeReadFetcher:
    name = "weread"

    def fetch(self, source: "SourceRow") -> FetchResult:
        if not source.url:
            return FetchResult(items=[], error="empty url")

        target, sub = _parse_source_url(source.url)
        if target is None:
            return FetchResult(
                items=[],
                error=f"weread-bad-url: {source.url!r} "
                      f"(use weread://shelf | weread://book/<id> | weread://mpbook)",
            )

        # Items collected so far. Mutated in place by the shelf walker so a
        # mid-walk global error (e.g. 风控) still surfaces partial progress
        # instead of throwing away every successfully-fetched book.
        items: list[Item] = []
        try:
            with _client() as client:
                if target == "shelf":
                    self._fetch_all_subscribed_mp(client, items)
                elif target == "mpbook":
                    items.extend(self._fetch_book_articles(client, "mpbook"))
                else:  # target == "book"
                    items.extend(self._fetch_book_articles(client, sub))
        except WeReadCookieError as exc:
            return FetchResult(items=items, error=f"weread-auth: {exc}")
        except WeReadEndpointError as exc:
            return FetchResult(items=items, error=f"weread-endpoint: {exc}")
        except WeReadBlockedError as exc:
            # Distinct from -auth so cron / status surfaces "manual re-login
            # required" rather than "just refresh cookie".
            return FetchResult(items=items, error=f"weread-blocked: {exc}")
        except WeReadUpstreamTimeout as exc:
            # Only reaches here on single-book paths; shelf walk swallows
            # per-book -10100 internally.
            return FetchResult(items=items, error=f"weread-timeout: {exc}")
        except WeReadResponseError as exc:
            return FetchResult(items=items, error=f"weread-response: {exc}")
        except httpx.HTTPError as exc:
            return FetchResult(items=items, error=f"weread-http: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            return FetchResult(items=items, error=f"weread-other: {exc!r}")

        return FetchResult(items=items)

    def _fetch_book_articles(self, client: httpx.Client, book_id: str) -> list[Item]:
        # MP_* → 公众号 article list; anything else → normal book chapters.
        if book_id.startswith("MP_"):
            return _mp_book_to_items(mp_articles_with_retry(client, book_id), book_id)
        return _normal_book_to_items(chapter_infos(client, [book_id]), book_id)

    def _fetch_all_subscribed_mp(
        self, client: httpx.Client, items: list[Item],
    ) -> None:
        """Walk shelf, fetch /web/mp/articles for every MP_* book.

        Mutates ``items`` in place so caller still sees partial progress on
        a global error (cookie / endpoint / 风控). Per-book transients
        (upstream timeout, HTTP blip) are swallowed locally so one bad
        公众号 doesn't kill the round.
        """
        shelf = shelf_sync(client)
        mp_book_ids = [
            b["bookId"] for b in (shelf.get("books") or [])
            if (b.get("bookId") or "").startswith("MP_") and b.get("type") == 3
        ]

        for i, bid in enumerate(mp_book_ids):
            if i > 0:
                time.sleep(random.uniform(*_PER_BOOK_JITTER_RANGE))
            try:
                items.extend(_mp_book_to_items(mp_articles_with_retry(client, bid), bid))
            except WeReadUpstreamTimeout:
                # Already retried once inside the helper — skip this 公众号
                # this round, next cycle picks it up.
                continue
            except httpx.HTTPError:
                continue
            # WeReadCookieError / WeReadEndpointError / WeReadBlockedError /
            # WeReadResponseError propagate up — they're global, not per-book.


# ---------- source URL parsing ----------

def _parse_source_url(url: str) -> tuple[str | None, str]:
    """Split ``weread://...`` into (target, subpath) per source-URL convention.

    Returns ``(target, sub)`` with target ∈ {``"shelf"``, ``"mpbook"``, ``"book"``}
    and ``sub`` the path remainder for ``book/<id>`` (empty otherwise). Returns
    ``(None, "")`` for any URL that doesn't fit the convention.
    """
    parsed = urlparse(url)
    if parsed.scheme != "weread":
        return None, ""
    target = parsed.netloc
    sub = parsed.path.strip("/")
    if target in ("shelf", "mpbook") and not sub:
        return target, ""
    if target == "book" and sub:
        return target, sub
    return None, ""


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
            # WeRead /web/mp/articles occasionally encodes base64url's `_` as
            # `~` in originalId (~10% of items). mp.weixin's strict base64url
            # parser then rejects the URL with "参数错误" (verified 2026-05-07).
            # Restore `_` for a canonical mp.weixin URL.
            original_id = original_id.replace("~", "_")
            # mp.weixin canonical short id is exactly 22 chars (base64url of
            # 16 bytes). Anything else is a truncation or junk in the WeRead
            # response — skip rather than store an unclickable URL.
            if len(original_id) != 22:
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
