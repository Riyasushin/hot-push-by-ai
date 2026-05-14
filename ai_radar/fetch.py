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
from rich.table import Table

from ai_radar import config as cfg
from ai_radar import db
from ai_radar._env import load_dotenv
from ai_radar.fetchers import FETCHERS

load_dotenv()

LOCK_FILE_REL = "data/.fetch.lock"
DB_FILE_REL = "data/radar.db"

console = Console()


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
    run_id = db.start_fetch_run(conn, len(sources))

    table = Table(show_header=True, header_style="bold")
    table.add_column("tier", width=5)
    table.add_column("source", overflow="fold")
    table.add_column("got", justify="right", width=5)
    table.add_column("new", justify="right", width=5)
    table.add_column("status")

    total_new = 0
    errors: list[dict] = []

    # try/finally so fetch_runs row never stays "started but never finished".
    # Even if we get killed mid-loop, finally runs and writes a record with
    # whatever progress we made.
    try:
        for src in sources:
            fetcher = FETCHERS.get(src.fetcher)
            if fetcher is None:
                table.add_row(src.tier, src.name, "-", "-", "[dim]no fetcher[/dim]")
                continue

            try:
                result = fetcher.fetch(src)
            except Exception as exc:  # noqa: BLE001
                table.add_row(src.tier, src.name, "-", "-", f"[red]error: {exc!r}[/red]")
                errors.append({"source": src.name, "error": repr(exc)})
                continue

            if result.error:
                errors.append({"source": src.name, "error": result.error})

            if result.not_modified:
                db.update_source_cache(
                    conn, src.id,
                    etag=result.etag, last_modified=result.last_modified,
                    fetched_at=datetime.now(timezone.utc),
                )
                table.add_row(src.tier, src.name, "0", "0", "[dim]304 not modified[/dim]")
                continue

            new = db.insert_items(conn, src.id, result.items)
            total_new += new
            db.update_source_cache(
                conn, src.id,
                etag=result.etag, last_modified=result.last_modified,
                fetched_at=datetime.now(timezone.utc),
            )

            status = f"[red]{result.error}[/red]" if result.error else "[green]ok[/green]"
            table.add_row(src.tier, src.name, str(len(result.items)), str(new), status)

        console.print(table)
        console.print(f"[bold]new items: {total_new}[/bold] ({len(errors)} errors)")
    finally:
        db.finish_fetch_run(conn, run_id, new_items=total_new, errors=errors)
        conn.close()
        lock_fh.close()
    # Per-source errors don't fail the cron — they're recorded in fetch_runs.errors.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
