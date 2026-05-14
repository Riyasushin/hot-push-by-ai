"""Top-level CLI.

    uv run radar fetch
    uv run radar status
    uv run radar sources
"""

from __future__ import annotations

import os
from datetime import datetime

import time

import typer
from rich.console import Console
from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TextColumn, TimeElapsedColumn,
)
from rich.table import Table

from ai_radar import config as cfg
from ai_radar import db
from ai_radar._env import load_dotenv
from ai_radar.fetch import DB_FILE_REL, main as fetch_main
from ai_radar.pipeline.prefilter import Prefilter
from ai_radar.pipeline.score import Scorer
from ai_radar.pipeline.weight import Weighter

# Auto-load ./.env so commands work without manually exporting vars in the shell.
load_dotenv()

app = typer.Typer(help="ai-radar — personal AI/econ news aggregator", no_args_is_help=True)
console = Console()


def _stale_minutes() -> int:
    """RADAR_CLAIM_STALE_MINUTES — how long a claim can sit before another worker reaps it.

    Default 60 covers a worst-case `radar score --limit 200` (~10–20min) plus
    headroom; bump to 180+ for very long catch-up runs so an in-flight worker
    isn't pre-empted mid-batch.
    """
    raw = os.environ.get("RADAR_CLAIM_STALE_MINUTES")
    if not raw:
        return 60
    try:
        v = int(raw)
    except ValueError:
        return 60
    return max(1, v)


def _progress_bar(label: str) -> Progress:
    """Progress widget shared by prefilter / score.

    Shows: spinner, label, current/total batches, elapsed time, plus a
    free-text status line that the callback updates per batch (latest
    item title, scored/skipped counts).
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=24),
        MofNCompleteColumn(),
        TextColumn("• elapsed"),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[status]}"),
        console=console,
        transient=False,
        refresh_per_second=4,
    )


@app.command()
def fetch(
    source: str | None = typer.Option(
        None, "--source", "-s",
        help="Only fetch sources whose name contains this substring "
             "(case-insensitive). Bypasses the active flag — useful for "
             "manually verifying a disabled source.",
    ),
) -> None:
    """Pull new items from every active source in sources.toml.

    \b
    What it does:
      - Sync sources.toml → DB (insert/update/soft-delete)
      - Hit each active source via its fetcher (rss / weread)
      - INSERT OR IGNORE into items (URL UNIQUE → idempotent)
      - Record fetch_runs row with errors for /  banner
      - fcntl flock on data/.fetch.lock — concurrent runs no-op

    \b
    Examples:
      radar fetch                       # one cycle, all active sources
      radar fetch --source paper-radar  # only that one (active flag ignored)
      # cron (every 2h):
      0 */2 * * *  cd /repo && uv run radar fetch
    """
    raise typer.Exit(fetch_main(name_filter=source))


@app.command()
def status() -> None:
    """Print DB health snapshot: items, scoring progress, last fetch, tier distribution.

    \b
    Sections (top to bottom):
      - items in DB           total rows in items table (all sources, all time)
      - scoring               how many scored vs pending; per-category breakdown
      - last fetch            most recent radar fetch run (started/finished/new/errors)
      - tier table            row count per tier (T1 / T1.5 / T2) for active sources

    Use this as a quick "is the pipeline healthy" check before debugging.
    """
    config = cfg.load_config()
    conn = db.connect(config.project_root / DB_FILE_REL)
    db.init_db(conn)

    n = db.item_count(conn)
    console.print(f"[bold]items in DB:[/bold] {n}")

    sstats = db.score_stats(conn)
    console.print(
        f"[bold]scoring:[/bold] {sstats['total']} scored, {sstats['pending']} pending"
    )
    if sstats["by_category"]:
        for cat, cnt in sstats["by_category"]:
            console.print(f"  [dim]{cat}: {cnt}[/dim]")

    last = db.latest_fetch_run(conn)
    if last:
        console.print(
            f"[bold]last fetch:[/bold] started={last['started_at']} "
            f"finished={last['finished_at']} new={last['new_items']} "
            f"errors={'yes' if last['errors'] else 'no'}"
        )
    else:
        console.print("[dim]no fetch_runs yet[/dim]")

    table = Table("tier", "items")
    for r in db.items_count_by_tier(conn):
        table.add_row(r["tier"], str(r["cnt"]))
    console.print(table)
    conn.close()


@app.command()
def prefilter(
    limit: int = typer.Option(30, "--limit", "-n", help="Max items to classify this run."),
    batch_size: int = typer.Option(20, "--batch-size", "-b",
                                   help="Items per kimi-cli invocation (bigger = fewer subprocess starts but more retry pain on failure)."),
) -> None:
    """Classify pending items as AI-related (1) or not (0) via kimi-cli.

    \b
    Iron Law A: this is the cheap step. Kimi runs locally, costs nothing.
    Failure modes leave is_ai_related at NULL → next run retries them.

    \b
    Examples:
      radar prefilter                  # default 30 items, batch 20
      radar prefilter --limit 200      # catch up on a backlog
      radar prefilter -n 50 -b 10      # smaller batches if kimi-cli is flaky
    """
    config = cfg.load_config()
    conn = db.connect(config.project_root / DB_FILE_REL)
    db.init_db(conn)

    pf = Prefilter(config.project_root, batch_size=batch_size)
    with _progress_bar("prefilter (kimi-cli)") as bar:
        task = bar.add_task("prefilter", total=1, status="loading…")
        cum = {"classified": 0, "fail": 0}

        def on_batch(i, total, outcome, batch):
            bar.update(task, total=total)
            if outcome.failed:
                cum["fail"] += 1
                bar.update(task, advance=1,
                           status=f"✗ #{i} failed: {(outcome.error or '')[:60]}")
            else:
                cum["classified"] += outcome.written
                bar.update(task, advance=1,
                           status=f"✓ #{i} ok · cum {cum['classified']} classified")

        stats = pf.run(conn, limit=limit, on_batch=on_batch, stale_minutes=_stale_minutes())

    console.print(f"[bold]prefilter done[/]")
    console.print(f"  pending requested:    {stats['pending']}")
    console.print(f"  classified AI=true:   [green]{stats['ai_yes']}[/green]")
    console.print(f"  classified AI=false:  [yellow]{stats['ai_no']}[/yellow]")
    console.print(f"  batches OK:           {stats['batches_ok']}")
    console.print(f"  batches failed:       [red]{stats['batches_failed']}[/red]")
    if stats["errors"]:
        for e in stats["errors"][:3]:
            console.print(f"    [red]{e}[/red]")
    conn.close()


@app.command()
def score(
    limit: int = typer.Option(20, "--limit", "-n", help="Max items to score this run."),
    batch_size: int = typer.Option(5, "--batch-size", "-b",
                                   help="Items per LLM call. Score prompt is heavy — keep small (3-5) for kimi to avoid timeouts."),
    backend: str = typer.Option("deepseek", "--backend",
                                help="deepseek (default, paid, fast) | kimi (one persistent kimi-cli session reused across batches, free) | kimi-once (legacy spawn-per-batch) | kimi-api (HTTP)."),
) -> None:
    """Score pending AI items on 4 dims + classify category + write summary_zh / reason.

    \b
    Iron Law A: this is the expensive step. DeepSeek is default because
    "what's hot in AI" is a world-knowledge problem — don't downgrade here.
    Use --backend kimi when you don't have a DeepSeek key.

    \b
    Each scored item gets: hardcore / primary_src / density / novelty (0-10),
    a category, a 1-2 line zh summary, and a recommendation reason. Total
    + is_selected come later via `radar weight`.

    \b
    Examples:
      radar score                          # default deepseek-v4-flash, 20 items
      radar score --limit 100              # catch up
      radar score --backend kimi           # free, kimi-cli subprocess (~5-15s/batch optimized)
      radar score --backend kimi-api       # opt-in HTTP path (slow if you have a reverse-tunnel proxy)
      DEEPSEEK_MODEL=deepseek-v4-pro radar score   # higher-quality scoring
    """
    config = cfg.load_config()
    conn = db.connect(config.project_root / DB_FILE_REL)
    db.init_db(conn)

    if backend == "kimi":
        # Default kimi backend = ONE persistent kimi-cli session reused across
        # all batches via stream-json protocol; ``/clear`` between prompts
        # wipes context so each batch is independent. Saves ~1-3s subprocess
        # startup × N batches AND avoids per-batch transient rc=1 (auth blip,
        # net flap) that compounds across many spawns. See
        # KimiCLIPersistentBackend docstring for the protocol details.
        from ai_radar.pipeline._llm import KimiCLIPersistentBackend
        be = KimiCLIPersistentBackend(
            cwd=str(config.project_root), thinking=False, timeout_s=180
        )
    elif backend == "kimi-once":
        # Legacy: spawn-per-batch subprocess. Kept as fallback in case the
        # persistent session has an issue.
        from ai_radar.pipeline._llm import KimiCLIBackend
        be = KimiCLIBackend(cwd=str(config.project_root), thinking=False, timeout_s=360)
    elif backend == "kimi-api":
        from ai_radar.pipeline._llm import KimiAPIBackend
        be = KimiAPIBackend(timeout_s=120)
    elif backend == "deepseek":
        be = None  # Scorer falls back to DeepSeekBackend()
    else:
        console.print(f"[red]unknown backend {backend!r}; pick deepseek | kimi | kimi-once | kimi-api[/red]")
        raise typer.Exit(2)

    scorer = Scorer(config.project_root, batch_size=batch_size, backend=be)
    with _progress_bar(f"scoring [{scorer.backend.name}]") as bar:
        task = bar.add_task("scoring", total=1, status="loading pending items…")
        cum = {"scored": 0}

        def on_batch(i, total, outcome, batch):
            bar.update(task, total=total)
            if outcome.failed:
                bar.update(task, advance=1,
                           status=f"✗ #{i} failed: {(outcome.error or '')[:60]}")
            else:
                cum["scored"] += outcome.written
                head = (batch[0].title or "")[:32] if batch else ""
                bar.update(task, advance=1,
                           status=f"✓ #{i} +{outcome.written} (cum {cum['scored']}) — {head}")

        stats = scorer.run(conn, limit=limit, on_batch=on_batch, stale_minutes=_stale_minutes())

    console.print(f"[bold]scoring done[/]  model={stats['model']}")
    console.print(f"  pending requested: {stats['pending']}")
    console.print(f"  scored:            [green]{stats['scored']}[/green]")
    console.print(f"  batches OK:        {stats['batches_ok']}")
    console.print(f"  batches failed:    [red]{stats['batches_failed']}[/red]")
    if stats["errors"]:
        for e in stats["errors"][:3]:
            console.print(f"    [red]{e}[/red]")
    conn.close()


@app.command()
def report(
    date: str = typer.Option(None, "--date", "-d", help="YYYY-MM-DD; defaults to today UTC. Overwrites if exists."),
    all_days: bool = typer.Option(False, "--all", help="Regenerate all days (last 30) that have selected items."),
    last: int = typer.Option(0, "--last", help="Regenerate the most recent N days that have content (0 = off)."),
) -> None:
    """Generate reports/YYYY-MM-DD.md from selected items.

    \b
    Examples:
      radar report                       # today (UTC)
      radar report --date 2026-05-06     # specific day
      radar report --last 7              # last 7 days that have content
      radar report --all                 # every day with content in last 30
    """
    from ai_radar.pipeline.report import write_report
    config = cfg.load_config()

    if (date and (all_days or last)) or (all_days and last):
        console.print("[red]pick one of: --date | --last N | --all[/red]")
        raise typer.Exit(2)

    if all_days or last:
        days = 30 if all_days else last
        conn = db.connect(config.project_root / DB_FILE_REL)
        db.init_db(conn)
        dates = db.daily_dates_with_content(conn, days=days)
        if not dates:
            console.print(f"[yellow]no days with selected items in last {days} days[/yellow]")
            return
        for d in dates:
            out = write_report(config.project_root, date_str=d)
            console.print(f"[green]wrote[/] {out}")
        console.print(f"[green]done[/] · {len(dates)} reports")
        return

    out = write_report(config.project_root, date_str=date)
    console.print(f"[green]wrote[/] {out}")


@app.command(name="weread-list")
def weread_list() -> None:
    """Probe WeRead with WEREAD_COOKIE; print all subscribed 公众号 + bookIds.

    \b
    What you get:
      - Lumped 文章收藏 bookId (rarely useful)
      - Full shelf table: bookId / type / title / author / lastChapterCreateTime
      - Raw JSON saved to data/weread_shelf.json for offline inspection

    \b
    Workflow:
      1. Set WEREAD_COOKIE in .env (see README — DevTools Network tab)
      2. radar weread-list                       # discover bookIds
      3. Copy MP_WXS_* bookIds into sources.toml
      4. radar fetch                             # next cycle picks them up
    """
    import json
    from ai_radar.fetchers import weread as wr

    try:
        with wr._client() as client:
            console.print("[dim]GET /web/shelf/sync ...[/dim]")
            shelf = wr.shelf_sync(client)
    except wr.WeReadCookieError as exc:
        console.print(f"[red]cookie problem:[/red] {exc}")
        console.print("[dim]→ re-grab WEREAD_COOKIE from DevTools Network tab[/dim]")
        raise typer.Exit(1)
    except wr.WeReadEndpointError as exc:
        console.print(f"[red]endpoint problem:[/red] {exc}")
        console.print("[dim]→ WeRead API may have moved; check fetchers/weread.py[/dim]")
        raise typer.Exit(1)
    except wr.WeReadResponseError as exc:
        console.print(f"[red]bad response:[/red] {exc}")
        raise typer.Exit(1)

    # Lumped 文章收藏
    mp = shelf.get("mp", {})
    if isinstance(mp, dict) and mp.get("book"):
        b = mp["book"]
        console.print(f"\n[bold]文章收藏 (lumped):[/bold] bookId={b.get('bookId')!r} "
                      f"title={b.get('title')!r}")

    # Books – list everything and let user pick (mp markers are hard to detect a priori)
    books = shelf.get("books", []) or []
    console.print(f"\n[bold]Books on shelf:[/bold] {len(books)} total\n")
    table = Table("bookId", "type", "extra_type", "title", "author", "lastChapterCreateTime")
    for b in books:
        ts = b.get("lastChapterCreateTime")
        ts_s = (datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "—")
        table.add_row(
            str(b.get("bookId", "")),
            str(b.get("type", "")),
            str(b.get("extra_type", "")),
            (b.get("title") or "")[:24],
            (b.get("author") or "")[:18],
            ts_s,
        )
    console.print(table)
    console.print(
        "\n[dim]Tip: 公众号 通常 type=1 / extra_type=1, bookId 形如 'MP_'.[/dim]"
        "\n[dim]复制感兴趣的 bookId 到 sources.toml: "
        "url = \"weread://book/<bookId>\"[/dim]"
    )

    # Save raw shelf JSON for offline inspection.
    out = config_path() / "data" / "weread_shelf.json"
    out.write_text(json.dumps(shelf, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"\n[green]raw shelf saved to {out}[/green]")


def config_path():
    return cfg.load_config().project_root


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address. Use 0.0.0.0 to expose on LAN."),
    port: int = typer.Option(8000, "--port", "-p", help="TCP port to listen on."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev only)."),
    root_path: str = typer.Option("", "--root-path", help="ASGI root_path when behind a reverse proxy that strips a prefix (e.g. /aihot)."),
) -> None:
    """Launch the FastAPI web app via uvicorn.

    \b
    Routes:
      /                精选时间线 (is_selected=1)
      /all             全部已评分, 分页 (page/size)
      /daily/<date>    某天日报 + 最近 14 天有内容的导航条
      /category/<cat>  按类别筛选精选
      POST /feedback/<id>   写 feedback 表 (👍 / 👎 / saved / hidden)

    \b
    Examples:
      radar serve                                            # 127.0.0.1:8000 (本机)
      radar serve --host 0.0.0.0 --port 18086                # LAN, 偏远端口
      radar serve --reload                                   # dev: 改代码自动 restart
      radar serve --root-path /aihot                         # 挂在反代子路径下 (Caddy handle_path /aihot/*)
    """
    import uvicorn
    uvicorn.run("ai_radar.web.app:app", host=host, port=port, reload=reload, root_path=root_path)


@app.command()
def weight() -> None:
    """Apply weights.toml to every scored item: compute total + flip is_selected.

    \b
    Pure code, no LLM call. Idempotent — rerun any time after editing
    weights.toml and the table updates instantly. Cheap (~ms even on 10k rows).

    \b
    Formula:
      weighted_avg = Σ(dim_i × w_i) / Σ(w_i)         # 0-10 scale
      total        = weighted_avg × tier_multiplier   # T1=1.20 / T1.5=1.00 / T2=0.85
      is_selected  = (total ≥ thresholds[category][tier])

    \b
    Run after:
      - radar score    (new items got dims; total still NULL)
      - editing weights.toml (re-apply new weights / thresholds)

    \b
    Examples:
      radar weight                     # apply current weights.toml to all rows
    """
    config = cfg.load_config()
    conn = db.connect(config.project_root / DB_FILE_REL)
    db.init_db(conn)

    w = Weighter(config.project_root)
    stats = w.run(conn)

    if "error" in stats:
        console.print(f"[red]error: {stats['error']}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[bold]weight done[/]  "
        f"selected [green]{stats['selected_total']}[/green] / {stats['scored_total']}"
    )
    table = Table("category", "selected", "total")
    for cat, sel, tot in sorted(stats["per_category"], key=lambda x: -x[1]):
        table.add_row(cat, str(sel), str(tot))
    console.print(table)
    conn.close()


@app.command()
def sources(all: bool = typer.Option(False, "--all", help="Include soft-deleted (active=0) sources too.")) -> None:
    """List sources from the DB mirror of sources.toml, with last-fetch timestamp.

    \b
    Columns: tier · category · fetcher · name · last_fetched · active
    Active is shown as ✓ / · — soft-deleted ones (gone from sources.toml) only
    appear when --all is passed; their items remain queryable in DB.

    \b
    Examples:
      radar sources                # active only
      radar sources --all          # include inactive (history)
    """
    config = cfg.load_config()
    conn = db.connect(config.project_root / DB_FILE_REL)
    db.init_db(conn)

    rows = db.all_sources(conn) if all else db.active_sources(conn)
    table = Table("tier", "category", "fetcher", "name", "last_fetched", "active")
    for s in rows:
        table.add_row(
            s.tier, s.category, s.fetcher, s.name,
            s.last_fetched_at or "-",
            "✓" if s.active else "·",
        )
    console.print(table)
    conn.close()


if __name__ == "__main__":
    app()
