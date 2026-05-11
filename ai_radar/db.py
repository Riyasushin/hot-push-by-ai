"""SQLite layer. Pure SQL, no business logic.

Schema migration strategy: idempotent CREATE TABLE IF NOT EXISTS at init time.
When a Step needs new columns, add them via ALTER TABLE in a small migration
function called from init_db(). Step 1 only needs the v1 schema below.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from ai_radar.config import Source
from ai_radar.fetchers.base import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    tier            TEXT NOT NULL,
    category        TEXT NOT NULL,
    url             TEXT NOT NULL,
    fetcher         TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    etag            TEXT,
    last_modified   TEXT,
    last_fetched_at TIMESTAMP,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    url           TEXT UNIQUE NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT,
    raw_content   TEXT,
    author        TEXT,
    published_at  TIMESTAMP,
    fetched_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_ai_related INTEGER,
    embedding     BLOB,
    event_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_items_source    ON items(source_id);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_ai        ON items(is_ai_related);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id            INTEGER PRIMARY KEY,
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    source_count  INTEGER,
    new_items     INTEGER,
    errors        TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    signal      TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_feedback_item   ON feedback(item_id);
CREATE INDEX IF NOT EXISTS idx_feedback_signal ON feedback(signal, created_at DESC);

CREATE TABLE IF NOT EXISTS scores (
    item_id      INTEGER PRIMARY KEY REFERENCES items(id),
    -- 4 LLM-judged dims (relevance_to_me dropped per user decision: taste lives in
    -- source curation + score prompt, not embedding).
    hardcore     REAL NOT NULL,
    primary_src  REAL NOT NULL,
    density      REAL NOT NULL,
    novelty      REAL NOT NULL,
    -- LLM-produced display payload (Iron law B: write at ingest, never recompute).
    category     TEXT NOT NULL,
    summary_zh   TEXT,
    reason       TEXT,
    scored_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    model        TEXT,
    -- Code-derived (Step 4 weight pass): total = weighted_avg × tier_multiplier;
    -- is_selected = 1 if total ≥ THRESHOLDS[category][tier] else 0.
    -- Recomputable any time by `radar weight` after editing weights.toml.
    total        REAL,
    is_selected  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_scores_category    ON scores(category);
"""

# Indexes added after migrations have run (so they reference columns that exist).
# idx_feedback_unique enforces "one row per (item, signal)" — toggle semantics.
# Pre-existing dupes must be cleaned before this index is added (one-time at DB
# init via _dedup_feedback).
_POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_scores_is_selected ON scores(is_selected, total DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_unique ON feedback(item_id, signal);
CREATE INDEX IF NOT EXISTS idx_items_dedup_key   ON items(dedup_key);
CREATE INDEX IF NOT EXISTS idx_items_claim       ON items(claim_owner, claim_at);
"""

# Per-table column migrations (idempotent).
_SCORES_MIGRATIONS = [
    ("total", "REAL"),
    ("is_selected", "INTEGER"),
]
_ITEMS_MIGRATIONS = [
    # 2026-05-07: dedup_key — same Zhihu article 赞同 by N users → N pin/<id> URLs
    # but we want to show the article once. Computed from title via
    # `_compute_dedup_key`. NULL means "no dedup needed" (uses url uniqueness).
    ("dedup_key", "TEXT"),
    # 2026-05-08: claim_owner / claim_at — multi-consumer pending dedup. The
    # prefilter / score steps atomically grab a batch via UPDATE...RETURNING
    # so two concurrent `radar prefilter` (or `radar score`) runs never read
    # the same id. See claim_pending_for_* below.
    ("claim_owner", "TEXT"),
    ("claim_at", "TIMESTAMP"),
]


def _ensure_columns(conn: sqlite3.Connection, table: str, migrations: list[tuple[str, str]]) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cur.fetchall()}
    for col, ddl in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _ensure_scores_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(conn, "scores", _SCORES_MIGRATIONS)


def _ensure_items_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(conn, "items", _ITEMS_MIGRATIONS)


@dataclass
class SourceRow:
    """Mirror of the sources table after sync. Includes DB-only fields."""

    id: int
    name: str
    tier: str
    category: str
    url: str
    fetcher: str
    active: bool
    etag: str | None
    last_modified: str | None
    last_fetched_at: str | None
    notes: str | None


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Wait for another writer (e.g. concurrent prefilter run, web server's
    # feedback POST) before raising "database is locked". 30s covers a kimi-cli
    # batch worst-case while we wait for its UPDATE burst to finish.
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_scores_columns(conn)
    _ensure_items_columns(conn)
    _dedup_feedback(conn)
    conn.executescript(_POST_MIGRATION_INDEXES)
    _backfill_dedup_keys(conn)


def _backfill_dedup_keys(conn: sqlite3.Connection) -> None:
    """One-shot: compute dedup_key for items inserted before the column existed.

    Cheap to skip when nothing's missing — bounded by an indexed scan looking
    for ``dedup_key IS NULL AND url LIKE '%zhihu.com%'``. Runs in Python because
    the action-prefix regex isn't expressible in SQLite without a UDF.
    """
    rows = conn.execute(
        "SELECT id, url, title FROM items "
        "WHERE dedup_key IS NULL AND url LIKE '%zhihu.com%'"
    ).fetchall()
    if not rows:
        return
    with transaction(conn):
        for r in rows:
            key = _compute_dedup_key(url=r["url"], title=r["title"] or "")
            if key:
                conn.execute(
                    "UPDATE items SET dedup_key = ? WHERE id = ?",
                    (key, r["id"]),
                )


# ---------- dedup_key for cross-source duplicate suppression ----------

import re as _re  # already imported at top? defensive

_ZHIHU_ACTION_RE = _re.compile(
    r"^.+?(?:赞同|收藏|关注|发表|发布|回答|分享)了"
    r"(?:回答|文章|想法|问题|圆桌|视频|专栏|内容)?\s*[:：]\s*(.+?)\s*$"
)


def _compute_dedup_key(*, url: str, title: str) -> str | None:
    """Return a dedup key for cross-source-aware deduplication.

    Currently handles Zhihu: same article 赞同 by N users → N ``zhihu.com/pin/<id>``
    URLs but the same canonical title once we strip the ``<user>赞同了回答:`` prefix.
    Returns the normalised title as the key, NULL for non-Zhihu items
    (URL uniqueness handles those).
    """
    if not title or "zhihu.com" not in (url or ""):
        return None
    m = _ZHIHU_ACTION_RE.match(title.strip())
    if not m:
        return None
    canonical = m.group(1).strip()
    if not canonical:
        return None
    return f"zhihu:{canonical[:200]}"


def _dedup_feedback(conn: sqlite3.Connection) -> None:
    # One-shot migration. Once idx_feedback_unique exists, the index itself
    # prevents dupes — no need to scan every startup.
    has_idx = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='index' AND name='idx_feedback_unique'"
    ).fetchone()
    if has_idx:
        return
    conn.execute(
        "DELETE FROM feedback WHERE id NOT IN "
        "(SELECT MIN(id) FROM feedback GROUP BY item_id, signal)"
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- sources ----------

def sync_sources(conn: sqlite3.Connection, sources: list[Source]) -> tuple[int, int, int]:
    """Sync toml-defined sources into the DB.

    Returns ``(inserted, updated, deactivated)``.
    Sources present in DB but absent from the toml list are soft-deleted (active=0)
    so historical items remain reachable.
    """
    toml_names = {s.name for s in sources}
    inserted = updated = deactivated = 0

    with transaction(conn):
        for s in sources:
            cur = conn.execute(
                "SELECT id, tier, category, url, fetcher, active, notes FROM sources WHERE name = ?",
                (s.name,),
            )
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO sources (name, tier, category, url, fetcher, active, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (s.name, s.tier, s.category, s.url, s.fetcher, int(s.active), s.notes),
                )
                inserted += 1
            else:
                changed = (
                    row["tier"] != s.tier
                    or row["category"] != s.category
                    or row["url"] != s.url
                    or row["fetcher"] != s.fetcher
                    or bool(row["active"]) != s.active
                    or (row["notes"] or None) != s.notes
                )
                if changed:
                    conn.execute(
                        """UPDATE sources SET tier=?, category=?, url=?, fetcher=?, active=?, notes=?
                           WHERE name = ?""",
                        (s.tier, s.category, s.url, s.fetcher, int(s.active), s.notes, s.name),
                    )
                    updated += 1

        cur = conn.execute("SELECT name FROM sources WHERE active = 1")
        for row in cur.fetchall():
            if row["name"] not in toml_names:
                conn.execute("UPDATE sources SET active = 0 WHERE name = ?", (row["name"],))
                deactivated += 1

    return inserted, updated, deactivated


def active_sources(conn: sqlite3.Connection) -> list[SourceRow]:
    cur = conn.execute(
        """SELECT id, name, tier, category, url, fetcher, active,
                  etag, last_modified, last_fetched_at, notes
           FROM sources WHERE active = 1 ORDER BY tier, name"""
    )
    return [
        SourceRow(
            id=r["id"], name=r["name"], tier=r["tier"], category=r["category"],
            url=r["url"], fetcher=r["fetcher"], active=bool(r["active"]),
            etag=r["etag"], last_modified=r["last_modified"],
            last_fetched_at=r["last_fetched_at"], notes=r["notes"],
        )
        for r in cur.fetchall()
    ]


def all_sources(conn: sqlite3.Connection) -> list[SourceRow]:
    cur = conn.execute(
        """SELECT id, name, tier, category, url, fetcher, active,
                  etag, last_modified, last_fetched_at, notes
           FROM sources ORDER BY active DESC, tier, name"""
    )
    return [
        SourceRow(
            id=r["id"], name=r["name"], tier=r["tier"], category=r["category"],
            url=r["url"], fetcher=r["fetcher"], active=bool(r["active"]),
            etag=r["etag"], last_modified=r["last_modified"],
            last_fetched_at=r["last_fetched_at"], notes=r["notes"],
        )
        for r in cur.fetchall()
    ]


def update_source_cache(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    etag: str | None,
    last_modified: str | None,
    fetched_at: datetime,
) -> None:
    conn.execute(
        """UPDATE sources SET etag = ?, last_modified = ?, last_fetched_at = ?
           WHERE id = ?""",
        (etag, last_modified, fetched_at.isoformat(timespec="seconds"), source_id),
    )


# ---------- items ----------

def upsert_item(conn: sqlite3.Connection, source_id: int, item: Item) -> bool:
    """INSERT OR IGNORE on items.url. Returns True iff a new row was inserted."""
    dedup_key = _compute_dedup_key(url=item.url, title=item.title)
    cur = conn.execute(
        """INSERT OR IGNORE INTO items
           (source_id, url, title, summary, raw_content, author, published_at, dedup_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_id,
            item.url,
            item.title,
            item.summary,
            item.raw_content,
            item.author,
            item.published_at.isoformat(timespec="seconds") if item.published_at else None,
            dedup_key,
        ),
    )
    return cur.rowcount > 0


def insert_items(conn: sqlite3.Connection, source_id: int, items: Iterable[Item]) -> int:
    new = 0
    for it in items:
        if upsert_item(conn, source_id, it):
            new += 1
    return new


def item_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


def items_count_by_tier(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """For ``radar status``: rows of (tier, cnt) for active sources."""
    return conn.execute(
        "SELECT s.tier, COUNT(i.id) AS cnt FROM sources s "
        "LEFT JOIN items i ON i.source_id = s.id "
        "WHERE s.active = 1 GROUP BY s.tier ORDER BY s.tier"
    ).fetchall()


def recent_items(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    cur = conn.execute(
        """SELECT i.title, i.url, i.published_at, s.name AS source, s.tier
           FROM items i JOIN sources s ON s.id = i.source_id
           ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
           LIMIT ?""",
        (limit,),
    )
    return cur.fetchall()


# ---------- fetch runs ----------

def start_fetch_run(conn: sqlite3.Connection, source_count: int) -> int:
    cur = conn.execute(
        "INSERT INTO fetch_runs (started_at, source_count) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), source_count),
    )
    return cur.lastrowid  # type: ignore[return-value]


def finish_fetch_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    new_items: int,
    errors: list[dict],
) -> None:
    conn.execute(
        """UPDATE fetch_runs SET finished_at = ?, new_items = ?, errors = ?
           WHERE id = ?""",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            new_items,
            json.dumps(errors, ensure_ascii=False) if errors else None,
            run_id,
        ),
    )


def latest_fetch_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT * FROM fetch_runs ORDER BY id DESC LIMIT 1",
    )
    return cur.fetchone()


def fetch_health(conn: sqlite3.Connection) -> dict:
    """Summarise the most recent fetch run for the web status banner.

    Returns:
        {
          "started_at": str,
          "finished_at": str | None,
          "new_items": int,
          "errors": list[{"source", "error"}],
          "weread_auth_expired": bool,    # cookie likely dead
          "rsshub_blocked": bool,         # rsshub instance returning 403/503/timeout
          "other_errors": int,
        }
    """
    row = latest_fetch_run(conn)
    if row is None:
        return {
            "started_at": None, "finished_at": None, "new_items": 0,
            "errors": [], "weread_auth_expired": False,
            "rsshub_blocked": False, "other_errors": 0,
        }

    raw_errors = row["errors"] or "[]"
    try:
        errors = json.loads(raw_errors) if isinstance(raw_errors, str) else []
    except json.JSONDecodeError:
        errors = []

    weread_auth = any(
        "weread-auth" in (e.get("error") or "").lower()
        for e in errors if isinstance(e, dict)
    )

    rsshub_blocked = any(
        ("rsshub" in (e.get("source") or "").lower()
         or "知乎" in (e.get("source") or "")
         or "X /" in (e.get("source") or ""))
        and any(s in (e.get("error") or "")
                for s in ("HTTP 403", "HTTP 503", "HTTP 502", "timeout", "ConnectError"))
        for e in errors if isinstance(e, dict)
    )

    other_errors = sum(
        1 for e in errors
        if isinstance(e, dict)
        and "weread-auth" not in (e.get("error") or "").lower()
        and "HTTP 403" not in (e.get("error") or "")
        and "HTTP 503" not in (e.get("error") or "")
    )

    return {
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "new_items": row["new_items"] or 0,
        "errors": errors,
        "weread_auth_expired": weread_auth,
        "rsshub_blocked": rsshub_blocked,
        "other_errors": other_errors,
    }


# ---------- claims (multi-consumer pending dedup) ----------
#
# Two `radar prefilter` (or `radar score`) processes running at once would
# otherwise both SELECT the same NULL/unscored rows → 2× LLM spend. The fix
# is to make the "pick a batch" step itself a write: UPDATE...RETURNING
# atomically tags rows with the caller's owner UUID before returning them.
# Concurrent callers serialise on SQLite's writer lock; each one walks away
# with a disjoint batch.
#
# Stale claims (caller crashed, machine rebooted) are reaped at the start of
# every run via release_stale_claims(); the threshold is RADAR_CLAIM_STALE_MINUTES
# (CLI default 60).

_PREFILTER_CLAIM_SQL = """
UPDATE items
   SET claim_owner = ?, claim_at = CURRENT_TIMESTAMP
 WHERE id IN (
       SELECT id FROM items
        WHERE is_ai_related IS NULL
          AND (claim_owner IS NULL OR claim_at < datetime('now', ?))
        ORDER BY fetched_at DESC
        LIMIT ?
 )
RETURNING id
"""

_SCORE_CLAIM_SQL = """
UPDATE items
   SET claim_owner = ?, claim_at = CURRENT_TIMESTAMP
 WHERE id IN (
       SELECT i.id FROM items i
        WHERE i.is_ai_related = 1
          AND NOT EXISTS (SELECT 1 FROM scores sc WHERE sc.item_id = i.id)
          AND (i.claim_owner IS NULL OR i.claim_at < datetime('now', ?))
        ORDER BY i.fetched_at DESC
        LIMIT ?
 )
RETURNING id
"""


def claim_pending_for_prefilter(
    conn: sqlite3.Connection,
    *,
    owner: str,
    limit: int,
    stale_minutes: int = 60,
) -> list[sqlite3.Row]:
    """Atomically claim up to N pending-prefilter items for ``owner``.

    Returns the same shape as the historical SELECT in pipeline/prefilter._load_pending
    (id, title, summary, source name) for the rows we won the race on.
    """
    cutoff = f"-{int(stale_minutes)} minutes"
    ids = [r["id"] for r in conn.execute(
        _PREFILTER_CLAIM_SQL, (owner, cutoff, int(limit))
    ).fetchall()]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT i.id, i.title, COALESCE(i.summary, '') AS summary, "
        f"       s.name AS source "
        f"FROM items i JOIN sources s ON s.id = i.source_id "
        f"WHERE i.id IN ({placeholders}) "
        f"ORDER BY i.fetched_at DESC",
        ids,
    ).fetchall()


def claim_pending_for_scoring(
    conn: sqlite3.Connection,
    *,
    owner: str,
    limit: int,
    stale_minutes: int = 60,
) -> list[sqlite3.Row]:
    """Atomically claim up to N pending-scoring items for ``owner``."""
    cutoff = f"-{int(stale_minutes)} minutes"
    ids = [r["id"] for r in conn.execute(
        _SCORE_CLAIM_SQL, (owner, cutoff, int(limit))
    ).fetchall()]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT i.id, i.title, COALESCE(i.summary, '') AS summary, "
        f"       s.name AS source_name, s.tier AS source_tier, "
        f"       s.category AS source_category "
        f"FROM items i JOIN sources s ON s.id = i.source_id "
        f"WHERE i.id IN ({placeholders}) "
        f"ORDER BY i.fetched_at DESC",
        ids,
    ).fetchall()


def release_claim(
    conn: sqlite3.Connection, *, item_ids: list[int], owner: str,
) -> None:
    """Drop our claim on these ids — only if we still own them.

    The owner-match guards against a stale-sweep having already given the
    rows away to another worker; in that case we mustn't yank them back.
    """
    if not item_ids:
        return
    placeholders = ",".join("?" * len(item_ids))
    conn.execute(
        f"UPDATE items SET claim_owner = NULL, claim_at = NULL "
        f"WHERE id IN ({placeholders}) AND claim_owner = ?",
        (*item_ids, owner),
    )


def release_stale_claims(
    conn: sqlite3.Connection, *, max_age_minutes: int,
) -> int:
    """Reap claims older than the threshold (caller crashed / machine died).

    Returns count of reclaimed rows. Cheap (idx_items_claim covers it).
    """
    cutoff = f"-{int(max_age_minutes)} minutes"
    cur = conn.execute(
        "UPDATE items SET claim_owner = NULL, claim_at = NULL "
        "WHERE claim_at IS NOT NULL AND claim_at < datetime('now', ?)",
        (cutoff,),
    )
    return cur.rowcount or 0


def mark_item_excluded(
    conn: sqlite3.Connection, *, item_id: int,
) -> None:
    """Permanently exclude an item from prefilter + score pending pools.

    Sets ``is_ai_related = -1`` (sentinel for "LLM refused / content_filter").
    Both ``_PREFILTER_CLAIM_SQL`` (matches IS NULL) and ``_SCORE_CLAIM_SQL``
    (matches = 1) skip this row going forward. Also clears any active claim.
    """
    conn.execute(
        "UPDATE items SET is_ai_related = -1, "
        "                  claim_owner = NULL, claim_at = NULL "
        "WHERE id = ?",
        (item_id,),
    )


def items_still_pending_prefilter(
    conn: sqlite3.Connection, *, item_ids: list[int],
) -> list[int]:
    """Subset of ids that still haven't been classified (is_ai_related IS NULL)."""
    if not item_ids:
        return []
    placeholders = ",".join("?" * len(item_ids))
    return [r["id"] for r in conn.execute(
        f"SELECT id FROM items "
        f"WHERE id IN ({placeholders}) AND is_ai_related IS NULL",
        item_ids,
    ).fetchall()]


def items_still_pending_scoring(
    conn: sqlite3.Connection, *, item_ids: list[int],
) -> list[int]:
    """Subset of ids that still don't have a scores row."""
    if not item_ids:
        return []
    placeholders = ",".join("?" * len(item_ids))
    return [r["id"] for r in conn.execute(
        f"SELECT i.id FROM items i "
        f"WHERE i.id IN ({placeholders}) "
        f"  AND NOT EXISTS (SELECT 1 FROM scores sc WHERE sc.item_id = i.id)",
        item_ids,
    ).fetchall()]


# ---------- scores ----------

def pending_for_scoring(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Items that passed the AI prefilter and have not been scored yet.

    Read-only — used by stats / web views. The pipeline itself uses
    ``claim_pending_for_scoring`` so concurrent runs don't double-process.
    """
    sql = (
        "SELECT i.id, i.title, COALESCE(i.summary, '') AS summary, "
        "       s.name AS source_name, s.tier AS source_tier, s.category AS source_category "
        "FROM items i JOIN sources s ON s.id = i.source_id "
        "LEFT JOIN scores sc ON sc.item_id = i.id "
        "WHERE i.is_ai_related = 1 AND sc.item_id IS NULL "
        "ORDER BY i.fetched_at DESC"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def upsert_score(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    hardcore: float,
    primary_src: float,
    density: float,
    novelty: float,
    category: str,
    summary_zh: str | None,
    reason: str | None,
    model: str,
) -> None:
    conn.execute(
        """INSERT INTO scores
           (item_id, hardcore, primary_src, density, novelty,
            category, summary_zh, reason, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
               hardcore    = excluded.hardcore,
               primary_src = excluded.primary_src,
               density     = excluded.density,
               novelty     = excluded.novelty,
               category    = excluded.category,
               summary_zh  = excluded.summary_zh,
               reason      = excluded.reason,
               model       = excluded.model,
               scored_at   = CURRENT_TIMESTAMP""",
        (item_id, hardcore, primary_src, density, novelty,
         category, summary_zh, reason, model),
    )


def score_stats(conn: sqlite3.Connection) -> dict:
    """Counts of items waiting / scored, by category."""
    pending = conn.execute(
        """SELECT COUNT(*) AS n FROM items i
           LEFT JOIN scores sc ON sc.item_id = i.id
           WHERE i.is_ai_related = 1 AND sc.item_id IS NULL"""
    ).fetchone()["n"]
    by_category = conn.execute(
        "SELECT category, COUNT(*) AS n FROM scores GROUP BY category ORDER BY n DESC"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) AS n FROM scores").fetchone()["n"]
    selected = conn.execute(
        "SELECT COUNT(*) AS n FROM scores WHERE is_selected = 1"
    ).fetchone()["n"]
    return {
        "pending": pending,
        "total": total,
        "selected": selected,
        "by_category": [(r["category"], r["n"]) for r in by_category],
    }


# ---------- weight / select ----------

def all_scored_with_source(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every scored item joined with its source tier — input to the weight pass."""
    return conn.execute(
        """SELECT sc.item_id, sc.hardcore, sc.primary_src, sc.density, sc.novelty,
                  sc.category, s.tier AS source_tier
           FROM scores sc
           JOIN items i  ON i.id = sc.item_id
           JOIN sources s ON s.id = i.source_id"""
    ).fetchall()


def update_total_and_selection(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    total: float,
    is_selected: int,
) -> None:
    conn.execute(
        "UPDATE scores SET total = ?, is_selected = ? WHERE item_id = ?",
        (total, is_selected, item_id),
    )


# ---------- timeline / display queries ----------

def selected_items(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Selected items, with cross-source dedup by `dedup_key` (Zhihu 赞同 etc.)
    collapsed to the highest-scoring representative + endorsement count.
    """
    where = ["sc.is_selected = 1"]
    params: list = []
    if category:
        where.append("sc.category = ?")
        params.append(category)
    # For dedup_key NULL → treat each row as its own group (id is the group key).
    # For dedup_key NOT NULL → keep the row with highest total per group.
    sql = f"""
        WITH ranked AS (
            SELECT i.id, i.url, i.title, i.published_at, i.fetched_at, i.dedup_key,
                   s.name AS source_name, s.tier AS source_tier,
                   s.category AS source_kind,
                   sc.hardcore, sc.primary_src, sc.density, sc.novelty,
                   sc.category, sc.summary_zh, sc.reason, sc.total, sc.model,
                   COUNT(*) OVER (PARTITION BY COALESCE(i.dedup_key, i.url))
                       AS endorsement_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(i.dedup_key, i.url)
                       ORDER BY sc.total DESC, i.id ASC
                   ) AS rn
            FROM scores sc
            JOIN items i  ON i.id = sc.item_id
            JOIN sources s ON s.id = i.source_id
            WHERE {' AND '.join(where)}
        )
        SELECT * FROM ranked WHERE rn = 1
        ORDER BY COALESCE(published_at, fetched_at) DESC, total DESC
        LIMIT ?
    """
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def all_scored_items(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Same dedup CTE as ``selected_items`` — collapses Zhihu 赞同 dupes to the
    highest-scoring representative, exposing ``endorsement_count``."""
    return conn.execute(
        """WITH ranked AS (
               SELECT i.id, i.url, i.title, i.published_at, i.fetched_at, i.dedup_key,
                      s.name AS source_name, s.tier AS source_tier,
                      sc.hardcore, sc.primary_src, sc.density, sc.novelty,
                      sc.category, sc.summary_zh, sc.reason, sc.total, sc.is_selected,
                      COUNT(*) OVER (PARTITION BY COALESCE(i.dedup_key, i.url))
                          AS endorsement_count,
                      ROW_NUMBER() OVER (
                          PARTITION BY COALESCE(i.dedup_key, i.url)
                          ORDER BY sc.total DESC, i.id ASC
                      ) AS rn
               FROM scores sc
               JOIN items i  ON i.id = sc.item_id
               JOIN sources s ON s.id = i.source_id
           )
           SELECT * FROM ranked WHERE rn = 1
           ORDER BY COALESCE(published_at, fetched_at) DESC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()


def all_scored_count(conn: sqlite3.Connection) -> int:
    """Distinct dedup-collapsed count — matches all_scored_items pagination."""
    return conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(i.dedup_key, i.url)) AS n "
        "FROM scores sc JOIN items i ON i.id = sc.item_id"
    ).fetchone()["n"]


def daily_dates_with_content(
    conn: sqlite3.Connection,
    *,
    days: int = 14,
) -> list[str]:
    """Distinct YYYY-MM-DD strings (UTC) within the last ``days`` that have ≥1
    selected item. Newest first. Used by the /daily nav strip — only render
    days that actually have content.
    """
    return [
        r["d"] for r in conn.execute(
            """SELECT DISTINCT
                      substr(COALESCE(i.published_at, i.fetched_at), 1, 10) AS d
               FROM scores sc
               JOIN items i ON i.id = sc.item_id
               WHERE sc.is_selected = 1
                 AND substr(COALESCE(i.published_at, i.fetched_at), 1, 10)
                     >= date('now', ?)
               ORDER BY d DESC""",
            (f"-{int(days) - 1} days",),
        ).fetchall()
    ]


def items_for_day(
    conn: sqlite3.Connection,
    *,
    date_str: str,
) -> list[sqlite3.Row]:
    """Selected items whose published_at falls on date_str (YYYY-MM-DD), UTC."""
    return conn.execute(
        """SELECT i.id, i.url, i.title, i.published_at,
                  s.name AS source_name, s.tier AS source_tier,
                  sc.category, sc.summary_zh, sc.reason, sc.total
           FROM scores sc
           JOIN items i  ON i.id = sc.item_id
           JOIN sources s ON s.id = i.source_id
           WHERE sc.is_selected = 1
             AND substr(COALESCE(i.published_at, i.fetched_at), 1, 10) = ?
           ORDER BY sc.category, sc.total DESC""",
        (date_str,),
    ).fetchall()


_OPPOSITE_SIGNAL = {"thumbs_up": "thumbs_down", "thumbs_down": "thumbs_up"}


def toggle_feedback(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    signal: str,
    note: str | None = None,
) -> dict:
    """Toggle a feedback row. Returns the new state.

    Semantics:
      - Click same signal twice → row goes away (active=False).
      - For thumbs_up / thumbs_down: clicking one clears the opposite
        (mutual exclusion — they're opinions, can't hold both).
      - saved / hidden are independent of each other and of thumbs.

    Returns: {"active": bool, "cleared_opposite": bool}
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT id FROM feedback WHERE item_id = ? AND signal = ?",
            (item_id, signal),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM feedback WHERE id = ?", (row["id"],))
            return {"active": False, "cleared_opposite": False}

        cleared_opposite = False
        opp = _OPPOSITE_SIGNAL.get(signal)
        if opp:
            cur = conn.execute(
                "DELETE FROM feedback WHERE item_id = ? AND signal = ?",
                (item_id, opp),
            )
            cleared_opposite = cur.rowcount > 0
        conn.execute(
            "INSERT INTO feedback (item_id, signal, note) VALUES (?, ?, ?)",
            (item_id, signal, note),
        )
        return {"active": True, "cleared_opposite": cleared_opposite}


def feedback_for_items(
    conn: sqlite3.Connection, item_ids: Iterable[int]
) -> dict[int, set[str]]:
    """Map item_id → {signal, ...} for the given items. Empty for unknown ids."""
    ids = list(item_ids)
    if not ids:
        return {}
    placeholder = ",".join("?" * len(ids))
    out: dict[int, set[str]] = {i: set() for i in ids}
    cur = conn.execute(
        f"SELECT item_id, signal FROM feedback WHERE item_id IN ({placeholder})",
        ids,
    )
    for r in cur.fetchall():
        out[r["item_id"]].add(r["signal"])
    return out
