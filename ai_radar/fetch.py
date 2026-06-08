"""Main fetch entrypoint.

    uv run python -m ai_radar.fetch

Sync sources.toml -> DB, then dispatch each active source through its fetcher,
upsert items, update HTTP cache state, record a fetch_runs row. A fcntl file
lock prevents two cron runs from overlapping.
"""

from __future__ import annotations

import fcntl
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
    TextColumn, TimeElapsedColumn,
)
from rich.table import Table

from ai_radar import config as cfg
from ai_radar import db
from ai_radar._env import load_dotenv
from ai_radar.fetchers import FETCHERS

load_dotenv()

LOCK_FILE_REL = "data/.fetch.lock"
DB_FILE_REL = "data/radar.db"

console = Console()

_TIMEOUT_ABORT_THRESHOLD = 3


def _source_fetch_order(src: db.SourceRow) -> tuple[int, str, str]:
    """Fetch WeChat sources first; keep tier/name order inside groups."""
    if src.name.startswith("公众号 /"):
        priority = 0
    elif src.fetcher == "weread":
        priority = 1
    else:
        priority = 2
    return priority, src.tier, src.name


def _is_timeout_error(error: str | None) -> bool:
    if not error:
        return False
    err = error.lower()
    return any(
        marker in err
        for marker in (
            "timeout", "timed out", "readtimeout",
            "connecttimeout", "pooltimeout", "weread-timeout",
        )
    )


def _is_weread_account_error(error: str | None) -> bool:
    if not error:
        return False
    err = error.lower()
    return err.startswith("weread-auth:") or err.startswith("weread-blocked:")


def _progress() -> Progress:
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


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("[yellow]another fetch is running, exiting[/yellow]")
        sys.exit(0)
    return fh


def main(name_filter: str | None = None) -> int:
    config = cfg.load_config()
    root = config.project_root
    lock_fh = _acquire_lock(root / LOCK_FILE_REL)

    conn = db.connect(root / DB_FILE_REL)
    db.init_db(conn)

    inserted, updated, deactivated = db.sync_sources(conn, config.sources)
    console.print(
        f"[dim]sources sync: +{inserted} new, ~{updated} updated, "
        f"-{deactivated} deactivated[/dim]"
    )

    if name_filter:
        # Manual / debug pick: match name substring (case-insensitive), ignore
        # the active flag so disabled sources can be tested without flipping toml.
        needle = name_filter.lower()
        sources = [s for s in db.all_sources(conn) if needle in s.name.lower()]
        if not sources:
            console.print(f"[red]no source matches --source {name_filter!r}[/red]")
            conn.close()
            lock_fh.close()
            return 1
        console.print(
            f"[dim]--source {name_filter!r} → {len(sources)} matched: "
            + ", ".join(s.name for s in sources) + "[/dim]"
        )
    else:
        sources = db.active_sources(conn)
    sources = sorted(sources, key=_source_fetch_order)
    run_id = db.start_fetch_run(conn, len(sources))

    table = Table(show_header=True, header_style="bold")
    table.add_column("tier", width=5)
    table.add_column("source", overflow="fold")
    table.add_column("got", justify="right", width=5)
    table.add_column("new", justify="right", width=5)
    table.add_column("status")

    total_got = 0
    total_new = 0
    errors: list[dict] = []
    consecutive_timeouts = 0
    skipped_weread = False
    aborted = False

    # try/finally so fetch_runs row never stays "started but never finished".
    # Even if we get killed mid-loop, finally runs and writes a record with
    # whatever progress we made.
    try:
        with _progress() as progress:
            task = progress.add_task(
                "fetch",
                total=len(sources),
                status="starting",
            )

            for idx, src in enumerate(sources, start=1):
                progress.update(
                    task,
                    description=f"fetch [{idx}/{len(sources)}]",
                    status=(
                        f"current={src.name} | got={total_got} new={total_new} "
                        f"errors={len(errors)} timeouts={consecutive_timeouts}/"
                        f"{_TIMEOUT_ABORT_THRESHOLD}"
                    ),
                )

                if skipped_weread and src.fetcher == "weread":
                    table.add_row(
                        src.tier, src.name, "-", "-",
                        "[yellow]skipped: weread account blocked[/yellow]",
                    )
                    consecutive_timeouts = 0
                    progress.advance(task)
                    console.print(
                        f"[dim]{idx}/{len(sources)}[/dim] {src.name} "
                        "got=- new=- total=-/- status=skipped-weread-account"
                    )
                    continue

                fetcher = FETCHERS.get(src.fetcher)
                if fetcher is None:
                    table.add_row(src.tier, src.name, "-", "-", "[dim]no fetcher[/dim]")
                    consecutive_timeouts = 0
                    progress.advance(task)
                    console.print(
                        f"[dim]{idx}/{len(sources)}[/dim] {src.name} "
                        "got=- new=- total=-/- status=no-fetcher"
                    )
                    continue

                got = 0
                new_items = 0
                status_text = "ok"
                timeout_error = False

                try:
                    result = fetcher.fetch(src)
                except Exception as exc:  # noqa: BLE001
                    error = repr(exc)
                    errors.append({"source": src.name, "error": error})
                    status = f"[red]error: {error}[/red]"
                    status_text = f"error: {error}"
                    timeout_error = _is_timeout_error(error)
                    table.add_row(src.tier, src.name, "-", "-", status)
                else:
                    if result.error:
                        errors.append({"source": src.name, "error": result.error})
                        timeout_error = _is_timeout_error(result.error)

                    if result.not_modified:
                        db.update_source_cache(
                            conn, src.id,
                            etag=result.etag, last_modified=result.last_modified,
                            fetched_at=datetime.now(timezone.utc),
                        )
                        status = "[dim]304 not modified[/dim]"
                        status_text = "304 not modified"
                        table.add_row(src.tier, src.name, "0", "0", status)
                    else:
                        got = len(result.items)
                        new_items = db.insert_items(conn, src.id, result.items)
                        total_got += got
                        total_new += new_items
                        db.update_source_cache(
                            conn, src.id,
                            etag=result.etag, last_modified=result.last_modified,
                            fetched_at=datetime.now(timezone.utc),
                        )

                        status = f"[red]{result.error}[/red]" if result.error else "[green]ok[/green]"
                        status_text = result.error or "ok"
                        table.add_row(src.tier, src.name, str(got), str(new_items), status)

                if timeout_error:
                    consecutive_timeouts += 1
                else:
                    consecutive_timeouts = 0

                if src.fetcher == "weread" and _is_weread_account_error(status_text):
                    skipped_weread = True
                    console.print(
                        "[yellow]WeRead account-level error detected; "
                        "skipping remaining weread:// sources this run.[/yellow]"
                    )

                progress.advance(task)
                progress.update(
                    task,
                    status=(
                        f"last={src.name} | got={total_got} new={total_new} "
                        f"errors={len(errors)} timeouts={consecutive_timeouts}/"
                        f"{_TIMEOUT_ABORT_THRESHOLD}"
                    ),
                )
                console.print(
                    f"[dim]{idx}/{len(sources)}[/dim] {src.name} "
                    f"got={got} new={new_items} total={total_got}/{total_new} "
                    f"status={status_text}"
                )

                if consecutive_timeouts >= _TIMEOUT_ABORT_THRESHOLD:
                    aborted = True
                    msg = (
                        f"abort: {_TIMEOUT_ABORT_THRESHOLD} consecutive timeouts; "
                        f"last source={src.name}"
                    )
                    errors.append({"source": "fetch", "error": msg})
                    console.print(f"[red]{msg}[/red]")
                    break

        console.print(table)
        console.print(
            f"[bold]fetched items: {total_got}[/bold]; "
            f"[bold]new items: {total_new}[/bold] ({len(errors)} errors)"
        )
    finally:
        db.finish_fetch_run(conn, run_id, new_items=total_new, errors=errors)
        conn.close()
        lock_fh.close()
    # Per-source errors do not fail cron unless we abort on repeated timeouts.
    return 2 if aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
