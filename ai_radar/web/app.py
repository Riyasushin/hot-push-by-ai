"""ai-radar web — single-user FastAPI app.

Routes:
    GET  /                精选时间线 (是 is_selected=1 的条目, 按 published_at DESC)
    GET  /all             全部已评分 AI 条目 (含未精选, 看噪音是否合理)
    GET  /daily           今日 (UTC) 日报视图
    GET  /daily/<date>    某天日报 (YYYY-MM-DD)
    GET  /category/<cat>  按类别筛选精选
    POST /feedback/<id>   写 feedback 表 (signal=thumbs_up | thumbs_down | hidden | saved)

No auth — single-user local app. Renders server-side with Jinja2.

Run via: ``uv run radar serve [--host 0.0.0.0 --port 8000]``
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Path as PathParam, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ai_radar import config as cfg
from ai_radar import db
from ai_radar.fetch import DB_FILE_REL

ALLOWED_FEEDBACK_SIGNALS = {"thumbs_up", "thumbs_down", "hidden", "saved"}

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _BASE_DIR / "templates"
_STATIC_DIR = _BASE_DIR / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="ai-radar", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["fmt_date"] = _fmt_date
    templates.env.filters["fmt_datetime"] = _fmt_datetime
    templates.env.filters["round1"] = lambda v: f"{float(v):.1f}" if v is not None else "—"

    config = cfg.load_config()
    db_path = config.project_root / DB_FILE_REL
    categories = config.weights.categories or [
        "论文研究", "infra工程", "模型发布", "产品发布", "行业经济", "技巧与观点",
    ]

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ---------- helpers ----------

    def _conn():
        c = db.connect(db_path)
        db.init_db(c)
        return c

    def _ctx(request: Request, **kwargs) -> dict:
        ctx = {
            "request": request,
            "categories": categories,
            "now_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        # Fetch health is cheap (one row + JSON parse) — every page renders it.
        # Done once per route here rather than per-template, so partials don't
        # have to hit the DB themselves.
        if "health" not in kwargs:
            with closing(_conn()) as c:
                ctx["health"] = db.fetch_health(c)
        ctx.update(kwargs)
        return ctx

    # ---------- routes ----------

    def _render(request: Request, template_name: str, **ctx) -> HTMLResponse:
        # Starlette 1.0+ wants (request, name, context). Wrapper keeps callers tidy.
        return templates.TemplateResponse(
            request, template_name, _ctx(request, **ctx)
        )

    def _with_feedback(c, items):
        fb = db.feedback_for_items(c, [it["id"] for it in items])
        # Jinja can index dicts; convert sets to lists for template.
        return {i: sorted(s) for i, s in fb.items()}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        with closing(_conn()) as c:
            items = db.selected_items(c, limit=100)
            stats = db.score_stats(c)
            feedback = _with_feedback(c, items)
        return _render(request, "timeline.html",
                       items=items, stats=stats, feedback=feedback,
                       view="精选", view_key="home")

    @app.get("/all", response_class=HTMLResponse)
    def all_view(request: Request, page: int = 1, size: int = 50):
        page = max(1, page)
        size = max(10, min(200, size))   # bounds: don't let page=1&size=99999 nuke RAM
        offset = (page - 1) * size
        with closing(_conn()) as c:
            items = db.all_scored_items(c, limit=size, offset=offset)
            total = db.all_scored_count(c)
            stats = db.score_stats(c)
            feedback = _with_feedback(c, items)
        pages = max(1, (total + size - 1) // size)
        pagination = {
            "page": page, "size": size, "total": total, "pages": pages,
            "has_prev": page > 1, "has_next": page < pages,
            "prev": page - 1, "next": page + 1,
        }
        return _render(request, "timeline.html",
                       items=items, stats=stats, feedback=feedback,
                       pagination=pagination,
                       view=f"全部 AI 已评分 (page {page}/{pages})", view_key="all",
                       show_unselected=True)

    @app.get("/category/{cat}", response_class=HTMLResponse)
    def category_view(request: Request, cat: str = PathParam(...)):
        if cat not in categories:
            raise HTTPException(status_code=404, detail=f"unknown category {cat!r}")
        with closing(_conn()) as c:
            items = db.selected_items(c, category=cat, limit=200)
            stats = db.score_stats(c)
            feedback = _with_feedback(c, items)
        return _render(request, "timeline.html",
                       items=items, stats=stats, feedback=feedback,
                       view=f"类别: {cat}", view_key=f"cat:{cat}")

    @app.get("/daily", response_class=HTMLResponse)
    def daily_today(request: Request):
        # Default: most recent day with content (or today if DB empty).
        with closing(_conn()) as c:
            dates = db.daily_dates_with_content(c, days=14)
        target = dates[0] if dates else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return RedirectResponse(url=f"/daily/{target}", status_code=302)

    @app.get("/daily/{date}", response_class=HTMLResponse)
    def daily_date(request: Request, date: str):
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        with closing(_conn()) as c:
            rows = db.items_for_day(c, date_str=date)
            available = db.daily_dates_with_content(c, days=14)
        by_cat: dict[str, list] = {cat: [] for cat in categories}
        for r in rows:
            by_cat.setdefault(r["category"], []).append(r)
        # available is newest-first → "newer" sits to the left in the index list.
        idx = available.index(date) if date in available else None
        newer = available[idx - 1] if idx is not None and idx > 0 else None
        older = available[idx + 1] if idx is not None and idx + 1 < len(available) else None
        nav = {"available": available, "newer": newer, "older": older, "current": date}
        return _render(request, "daily.html", date=date, by_cat=by_cat, nav=nav,
                       view=f"日报 · {date}", view_key="daily")

    @app.post("/feedback/{item_id}")
    def feedback(item_id: int, signal: str = Form(...), note: str | None = Form(None)):
        if signal not in ALLOWED_FEEDBACK_SIGNALS:
            raise HTTPException(status_code=400, detail=f"bad signal {signal!r}")
        with closing(_conn()) as c:
            state = db.toggle_feedback(c, item_id=item_id, signal=signal, note=note)
        return {"ok": True, "item_id": item_id, "signal": signal, **state}

    return app


# ---------- jinja filters ----------

def _fmt_date(value) -> str:
    if not value:
        return "—"
    return str(value)[:10]


def _fmt_datetime(value) -> str:
    if not value:
        return "—"
    return str(value)[:16].replace("T", " ")


app = create_app()
