"""起点中文网 fetcher.

不每章一条进 timeline; 而是按"领先 N 章才推一次"语义聚合, 每本书最多
emit 一个 Item, title 只含书名, summary 给积压数. 这样 entertainment 页
里一本书最多一行 "《xxx》 积压了 8 章未读".

数据完全来自 data/qidian_progress.json (由 scripts/qidian-progress-sync.py
从 https://my.qidian.com/bookcase 抓的, 提供 latest_chapter + last_read_chapter).
fetcher 自身不做网络 I/O — 起点书架接口要 cookie, 让 sync 脚本一处管 cookie
更干净; fetcher 只是个比大小+ emit 的薄壳.

URL 约定 (sources.toml):
    qidian://book/<bookId>

Dedup 策略: 输出 Item 的 url 嵌入 last_read 数字 (?from=N), 用户进度变了
url 就变, 触发新行; 没变就还是同一 url, items.url UNIQUE 自动挡掉重复.

progress.json 没该书条目时 = 视作已追完 → 不 emit. 等 sync 脚本把
latest/last_read seed 进来.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ai_radar.fetchers.base import FetchResult, Item

if TYPE_CHECKING:
    from ai_radar.db import SourceRow


_PROGRESS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "qidian_progress.json"

# ---- 章节号解析: sync 脚本复用本模块的 _extract_chapter_index / _cn_to_int ----
# (sync 抓 bookcase 时, 已读/最新章节都以 "第N章 标题" 文本形式给, 也分阿拉伯/中文)

_CHAPTER_RE = re.compile(r"第\s*([\d零〇一二三四五六七八九十百千万两]+)\s*章")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "两": 2}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn_to_int(s: str) -> int | None:
    """中文数字 → int (覆盖 1-99999). '一' → 1, '十' → 10, '一百二十三' → 123,
    '一万零五百' → 10500. 无法解析返回 None."""
    if not s:
        return None
    total = 0
    section = 0
    current_digit = 0
    for ch in s:
        if ch in _CN_DIGITS:
            current_digit = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if current_digit == 0 and unit == 10:
                section += 10
            else:
                section += current_digit * unit
            current_digit = 0
        elif ch == "万":
            section += current_digit
            current_digit = 0
            total = (total + section) * 10000
            section = 0
        else:
            return None
    return total + section + current_digit


def _extract_chapter_index(title: str) -> int | None:
    m = _CHAPTER_RE.search(title or "")
    if not m:
        return None
    s = m.group(1)
    if s.isdigit():
        return int(s)
    return _cn_to_int(s)


# ---- fetcher ----

def _parse_source_url(url: str) -> str | None:
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if parts.scheme != "qidian" or parts.netloc != "book":
        return None
    book_id = parts.path.lstrip("/")
    return book_id or None


def _load_progress() -> dict:
    if not _PROGRESS_PATH.exists():
        return {"books": {}, "default_offset": 20}
    try:
        with _PROGRESS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"books": {}, "default_offset": 20}
    if not isinstance(data, dict):
        return {"books": {}, "default_offset": 20}
    data.setdefault("books", {})
    data.setdefault("default_offset", 20)
    return data


def _parse_synced_at(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


class QidianFetcher:
    name = "qidian"

    def fetch(self, source: "SourceRow") -> FetchResult:
        if not source.url:
            return FetchResult(items=[], error="empty url")

        book_id = _parse_source_url(source.url)
        if book_id is None:
            return FetchResult(
                items=[],
                error=f"qidian-bad-url: {source.url!r} (want qidian://book/<bookId>)",
            )

        progress = _load_progress()
        book_state = progress["books"].get(book_id)
        if not book_state:
            # 还没 sync 过 — 不 emit, 不算错. sync 脚本下次跑会 seed 进来.
            return FetchResult(items=[])

        latest = book_state.get("latest_chapter")
        last_read = book_state.get("last_read_chapter")
        if latest is None or last_read is None:
            # 旧版 progress.json 格式 (只有 last_read) → 等下次 sync 补 latest
            return FetchResult(items=[])

        offset = int(book_state.get("offset", progress.get("default_offset", 20)))
        unread = int(latest) - int(last_read)
        if unread < offset:
            return FetchResult(items=[])

        book_name = book_state.get("name") or f"book/{book_id}"
        item = Item(
            url=f"https://m.qidian.com/book/{book_id}/catalog/?from={last_read}",
            title=book_name,
            summary=f"已积压 {unread} 章未读 (最新: 第 {latest} 章; 你读到第 {last_read} 章)",
            author=None,
            published_at=_parse_synced_at(book_state.get("last_synced_at"))
                          or datetime.now(timezone.utc),
        )
        return FetchResult(items=[item])
