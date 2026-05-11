#!/usr/bin/env python3
"""qidian-progress-sync.py — 同步起点账号的 "最近读到第几章" 到本地 progress.json.

策略: 抓 https://my.qidian.com/bookcase (服务端渲染的书架页面), 直接从 HTML
拿每本书的 "已读章节". 比起点的私有 ajax 稳: SPA 那套接口一年改两次, bookcase
HTML 形态多年没动 (服务端模板).

用法:
    python scripts/qidian-progress-sync.py --once     # cron 模式, 跑一次退出
    python scripts/qidian-progress-sync.py --probe    # 不写盘, 只打印解析结果
    python scripts/qidian-progress-sync.py --set <bookId> <chapter>  # 手动覆盖
    python scripts/qidian-progress-sync.py            # 长跑循环 (默认 30 分钟)

cron 行 (与 weread keepalive 同节奏):
    */30 * * * * cd /home/rj/Apps/ai-reader && \
        uv run python scripts/qidian-progress-sync.py --once \
            >> data/qidian-sync.log 2>&1

进度文件 data/qidian_progress.json:
    {"books": {"<bookId>": {"name":..., "last_read_chapter": N, "offset": M,
                            "last_synced_at": ...}},
     "default_offset": 20}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import httpx

# 复用 fetcher 里的 CN 数字解析, 不重复造轮子.
from ai_radar.fetchers.qidian import _extract_chapter_index


REPO_ROOT = Path(__file__).resolve().parent.parent
PROGRESS_PATH = REPO_ROOT / "data" / "qidian_progress.json"
ENV_PATH = REPO_ROOT / ".env"
SOURCES_PATH = REPO_ROOT / "sources.toml"

BOOKCASE_URL = "https://my.qidian.com/bookcase"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# bookcase 每本书一行 <tr>; 关键字段:
#   - data-bid="<bookId>" 锚书名
#   - <a class="shelf-table-chapter" ... title="第N章 标题">  最新章节 (作者更新到)
#   - <td class="col5"> ... title="读至第N章 标题">           已读章节 (你读到)
_BOOKID_RE = re.compile(r'value="(\d+)"')
_LATEST_TITLE_RE = re.compile(r'class="shelf-table-chapter[^"]*"[^>]*title="([^"]+)"')
_READAT_TITLE_RE = re.compile(r'<td class="col5"[^>]*>.*?title="读至([^"]+)"', re.DOTALL)
_BOOKNAME_RE = re.compile(r'data-bid="(\d+)"[^>]*>([^<]+)</a>')


# ---------- I/O ----------

def _read_cookie() -> str:
    if not ENV_PATH.exists():
        sys.exit(f"✗ {ENV_PATH} 不存在; 先创建并加 QIDIAN_COOKIE=...")
    with ENV_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip().lower() == "qidian_cookie":
                return v.strip().strip('"').strip("'")
    sys.exit(f"✗ {ENV_PATH} 里没有 QIDIAN_COOKIE; 浏览器登录 qidian.com → F12 复制 Cookie → 粘进去")


def _read_progress() -> dict:
    if not PROGRESS_PATH.exists():
        return {"books": {}, "default_offset": 20}
    try:
        with PROGRESS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"✗ {PROGRESS_PATH} 解析失败: {e}; 手动检查 / 删了重建")
    if not isinstance(data, dict):
        sys.exit(f"✗ {PROGRESS_PATH} 顶层不是 dict; 删了重建")
    data.setdefault("books", {})
    data.setdefault("default_offset", 20)
    return data


def _write_progress(data: dict) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(PROGRESS_PATH.parent),
                                     prefix=".qidian_progress.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, PROGRESS_PATH)
    except Exception:
        try: os.unlink(tmp_name)
        except OSError: pass
        raise


def _subscribed_book_ids() -> set[str]:
    if not SOURCES_PATH.exists():
        return set()
    with SOURCES_PATH.open("rb") as f:
        raw = tomllib.load(f)
    out: set[str] = set()
    for s in raw.get("source", []):
        if s.get("fetcher") != "qidian" or not s.get("active", True):
            continue
        m = re.match(r"^qidian://book/(\w+)/?$", s.get("url", ""))
        if m:
            out.add(m.group(1))
    return out


# ---------- bookcase parsing ----------

def _fetch_bookcase(cookie: str) -> str:
    with httpx.Client(follow_redirects=True, timeout=15.0) as client:
        resp = client.get(
            BOOKCASE_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Cookie": cookie,
                "Referer": "https://my.qidian.com/",
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"bookcase status={resp.status_code}; cookie 可能失效, 重新 F12 复制")
    if "nickName" not in resp.text:
        # 未登录会返回登录页, 没有 nickName 字段
        raise RuntimeError("bookcase 没有 nickName 字段; cookie 失效, 重新 F12 复制")
    return resp.text


def _parse_bookcase(html: str) -> dict[str, dict]:
    """{bookId: {name, last_read_chapter_index, latest_chapter_index}}.
    解析失败的字段缺省; 至少要有 latest 才保留 (没 latest 这本书也无从判断)."""
    out: dict[str, dict] = {}
    rows = re.split(r"<tr[ >]", html)
    for row in rows:
        m_bid = _BOOKID_RE.search(row)
        if not m_bid:
            continue
        bid = m_bid.group(1)
        m_latest = _LATEST_TITLE_RE.search(row)
        if not m_latest:
            continue
        latest_idx = _extract_chapter_index(m_latest.group(1))
        if latest_idx is None:
            continue
        rec: dict = {"latest_chapter_index": latest_idx}
        m_read = _READAT_TITLE_RE.search(row)
        if m_read:
            read_idx = _extract_chapter_index(m_read.group(1))
            if read_idx is not None:
                rec["last_read_chapter_index"] = read_idx
        m_name = _BOOKNAME_RE.search(row)
        if m_name:
            rec["name"] = m_name.group(2).strip()
        out[bid] = rec
    return out


# ---------- modes ----------

def cmd_probe(cookie: str) -> int:
    """抓 bookcase, 打印解析结果, 不写盘."""
    print(f"== 抓 {BOOKCASE_URL}")
    try:
        html = _fetch_bookcase(cookie)
    except Exception as e:
        print(f"✗ {e}")
        return 1
    parsed = _parse_bookcase(html)
    print(f"  解析出 {len(parsed)} 本书:")
    for bid, rec in parsed.items():
        latest = rec.get("latest_chapter_index", "?")
        read = rec.get("last_read_chapter_index", "?")
        gap = (latest - read) if isinstance(latest, int) and isinstance(read, int) else "?"
        print(f"    {bid}  latest {latest} / read {read}  (gap {gap})  {rec.get('name')}")
    subscribed = _subscribed_book_ids()
    print(f"\n== sources.toml 里 active 的 qidian 源: {len(subscribed)} 本")
    missing = subscribed - parsed.keys()
    if missing:
        print(f"  ⚠ 这些订了但 bookcase 没读过 (会被 fetcher 视作 caught up): {missing}")
    return 0


def cmd_sync(cookie: str) -> int:
    subscribed = _subscribed_book_ids()
    if not subscribed:
        print("⚠ sources.toml 里没有 active 的 fetcher='qidian' 源; sync 跳过")
        return 0
    try:
        html = _fetch_bookcase(cookie)
    except Exception as e:
        print(f"✗ {e}")
        return 1
    parsed = _parse_bookcase(html)
    progress = _read_progress()
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    updated = 0
    for bid in subscribed:
        rec = parsed.get(bid)
        if not rec:
            continue  # 订了但 bookcase 没出 (未加入书架?) → fetcher 视作 caught up
        entry = progress["books"].setdefault(bid, {})
        entry["name"] = rec.get("name") or entry.get("name") or f"book/{bid}"
        entry["latest_chapter"] = int(rec["latest_chapter_index"])
        # 没"已读" (你没读过) 视作 last_read = latest, 即默认追上.
        entry["last_read_chapter"] = int(rec.get("last_read_chapter_index", rec["latest_chapter_index"]))
        entry.setdefault("offset", progress.get("default_offset", 20))
        entry["last_synced_at"] = now
        updated += 1
    _write_progress(progress)
    print(f"✓ {now} synced from bookcase: {updated}/{len(subscribed)} books updated")
    return 0


def cmd_set(book_id: str, chapter: int, offset: int | None, name: str | None) -> int:
    progress = _read_progress()
    entry = progress["books"].setdefault(book_id, {})
    entry["last_read_chapter"] = int(chapter)
    entry["offset"] = int(offset) if offset is not None else entry.get("offset", progress.get("default_offset", 20))
    entry["name"] = name or entry.get("name") or f"book/{book_id}"
    entry["last_synced_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    _write_progress(progress)
    print(f"✓ {book_id} ← chapter={chapter} offset={entry['offset']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="跑一次 sync 退出 (cron 模式)")
    ap.add_argument("--probe", action="store_true", help="抓 bookcase + 打印解析结果, 不写盘")
    ap.add_argument("--set", nargs=2, metavar=("BOOKID", "CHAPTER"), help="手动设定某本书的进度")
    ap.add_argument("--offset", type=int, default=None, help="跟 --set 一起用, 单本 offset")
    ap.add_argument("--name", default=None, help="跟 --set 一起用, 显示名")
    ap.add_argument("--interval", type=int, default=int(os.environ.get("QIDIAN_SYNC_INTERVAL", 1800)),
                    help="长跑循环间隔秒 (默认 1800)")
    args = ap.parse_args()

    if args.set:
        return cmd_set(args.set[0], int(args.set[1]), args.offset, args.name)

    cookie = _read_cookie()

    if args.probe:
        return cmd_probe(cookie)
    if args.once:
        return cmd_sync(cookie)

    print(f"qidian-progress-sync starting (interval={args.interval}s)")
    while True:
        try:
            cmd_sync(cookie)
        except KeyboardInterrupt:
            print("stopped"); return 0
        except Exception as e:  # noqa: BLE001
            print(f"⚠ sync 异常: {e!r}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
