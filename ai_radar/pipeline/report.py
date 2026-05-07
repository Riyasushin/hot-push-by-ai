"""Daily Markdown report generator.

Reads selected items for a given date, buckets them by category, writes a
``reports/YYYY-MM-DD.md`` file. **No LLM call** — pure SELECT + template
(Iron Law B: 展示层零模型调用).

Usage::

    radar report                  # today (UTC)
    radar report --date 2026-05-07
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from ai_radar import db


def _bucket(rows) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for r in rows:
        out[r["category"]].append(r)
    return out


def render_daily(
    conn: sqlite3.Connection,
    *,
    date_str: str,
    categories_order: list[str],
) -> str:
    rows = db.items_for_day(conn, date_str=date_str)
    by_cat = _bucket(rows)

    total = sum(len(v) for v in by_cat.values())
    lines: list[str] = []
    lines.append(f"# AI Radar 日报 · {date_str}")
    lines.append("")
    lines.append(f"> 共 **{total}** 条精选 · 自动生成于 "
                 f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("")

    if total == 0:
        lines.append("_（这天没有过阈值的精选条目）_")
        return "\n".join(lines) + "\n"

    # TOC
    lines.append("## 目录")
    lines.append("")
    for cat in categories_order:
        n = len(by_cat.get(cat, []))
        if n:
            lines.append(f"- [{cat}](#{_slug(cat)}) · {n} 条")
    lines.append("")

    # Sections
    for cat in categories_order:
        items = by_cat.get(cat, [])
        if not items:
            continue
        lines.append(f"## {cat} <a id=\"{_slug(cat)}\"></a>")
        lines.append("")
        for it in items:
            score = it["total"]
            score_s = f"{float(score):.1f}" if score is not None else "—"
            tier = it["source_tier"]
            src = it["source_name"]
            published = (it["published_at"] or "")[:10]
            lines.append(f"### [{it['title']}]({it['url']})")
            lines.append("")
            lines.append(f"`{tier}` · {src} · 总分 **⭐ {score_s}**"
                         + (f" · {published}" if published else ""))
            lines.append("")
            if it["summary_zh"]:
                lines.append(it["summary_zh"])
                lines.append("")
            if it["reason"]:
                lines.append(f"> 💡 {it['reason']}")
                lines.append("")
        lines.append("")

    return "\n".join(lines) + "\n"


def write_report(
    project_root: Path,
    *,
    date_str: str | None = None,
) -> Path:
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from ai_radar import config as cfg
    config = cfg.load_config(project_root)
    categories = config.weights.categories or [
        "论文研究", "infra工程", "模型发布", "产品发布", "行业经济", "技巧与观点",
    ]
    conn = db.connect(project_root / "data/radar.db")
    db.init_db(conn)
    md = render_daily(conn, date_str=date_str, categories_order=categories)
    out_path = project_root / "reports" / f"{date_str}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    return out_path


def _slug(s: str) -> str:
    return s.replace(" ", "-").lower()
