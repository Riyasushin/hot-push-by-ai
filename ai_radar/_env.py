"""Tiny .env loader (no python-dotenv dep).

Supports the two formats we actually use:
    KEY=value
    export KEY=value

Comments (#...) and blank lines are skipped. Quoted values have surrounding
quotes stripped. Already-set environment variables are not overwritten —
shell env wins, file is fallback.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE_RE = re.compile(
    r"""^\s*
        (?:export\s+)?              # optional 'export'
        ([A-Za-z_][A-Za-z0-9_]*)    # KEY
        \s*=\s*
        (.*?)                       # value (greedy-trimmed below)
        \s*$""",
    re.VERBOSE,
)


def load_dotenv(path: Path | None = None) -> int:
    """Load env vars from path (default: ./.env). Returns count loaded."""
    p = path or Path(".env")
    if not p.exists():
        return 0

    count = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        # strip optional surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key in os.environ:           # shell env wins
            continue
        os.environ[key] = value
        count += 1
    return count
