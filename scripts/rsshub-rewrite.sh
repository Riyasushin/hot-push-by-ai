#!/usr/bin/env bash
# rsshub-rewrite.sh — repoint sources.toml RSSHub URLs.
#
# Usage:
#   bash scripts/rsshub-rewrite.sh http://localhost:1200
#   bash scripts/rsshub-rewrite.sh https://rsshub.app                 # revert
#   bash scripts/rsshub-rewrite.sh http://localhost:1200 --activate   # also flip
#                                                                       active=false
#                                                                       → true
#                                                                       on knock-on
#                                                                       Zhihu/X rows
#
# Backs up sources.toml → sources.toml.bak first. Idempotent.

set -uo pipefail

NEW_BASE="${1:-}"
ACTIVATE=0
[[ "${2:-}" == "--activate" ]] && ACTIVATE=1

if [[ -z "$NEW_BASE" ]]; then
    cat <<EOM >&2
✗ usage: $0 <new-rsshub-base-url> [--activate]

Examples:
  bash scripts/rsshub-rewrite.sh http://localhost:1200
  bash scripts/rsshub-rewrite.sh http://10.0.0.5:1200
  bash scripts/rsshub-rewrite.sh https://rsshub.app           # revert
EOM
    exit 1
fi

# Strip trailing slash if any
NEW_BASE="${NEW_BASE%/}"

# Validate it looks like a URL
if [[ ! "$NEW_BASE" =~ ^https?:// ]]; then
    echo "✗ base URL must start with http:// or https://" >&2
    exit 1
fi

[[ -f sources.toml ]] || { echo "✗ run from project root (sources.toml not found here)" >&2; exit 1; }

cp sources.toml sources.toml.bak
echo "✓ backed up sources.toml → sources.toml.bak"

# Count BEFORE rewrite
BEFORE=$(grep -cE 'https?://[^"]*rsshub' sources.toml || true)

# Rewrite all RSSHub host bases. Use python to avoid sed escaping headaches
# with ports / colons.
python3 - <<PY
import re, sys
p = "sources.toml"
text = open(p, encoding="utf-8").read()
new_base = "$NEW_BASE"

# Match all known RSSHub host forms we may have used:
hosts = [
    r"https?://rsshub\.app",
    r"https?://rsshub\.rssforever\.com",
    r"https?://rss\.shab\.fun",
    r"https?://rsshub\.feeded\.xyz",
    r"https?://rsshub\.atgw\.io",
    r"https?://rsshub-instance\.zeabur\.app",
    # Generic localhost-ish (so we can revert too)
    r"http://localhost:\d+",
    r"http://127\.0\.0\.1:\d+",
    r"http://10\.\d+\.\d+\.\d+:\d+",
    r"http://192\.168\.\d+\.\d+:\d+",
]
pattern = "|".join(hosts)

new_text, n = re.subn(rf"({pattern})(?=/)", new_base, text)
open(p, "w", encoding="utf-8").write(new_text)
print(f"REWRITES={n}")
PY

# Re-count AFTER
AFTER=$(grep -cE "$NEW_BASE/" sources.toml || true)

echo "✓ rewrote $AFTER URLs to base $NEW_BASE/"

# Optional: activate sources whose URL we just rewrote, if currently active=false
if (( ACTIVATE )); then
    echo "  --activate: flipping active=false → true on rewritten rows…"
    python3 - <<PY
import re
p = "sources.toml"
text = open(p, encoding="utf-8").read()
new_base = "$NEW_BASE"

def patch_block(m):
    blk = m.group(0)
    if new_base in blk and re.search(r'^active\s*=\s*false', blk, re.M):
        blk = re.sub(r'^active\s*=\s*false', 'active   = true', blk, flags=re.M)
    return blk

# Block = '[[source]]' header through next header or EOF
new_text = re.sub(r'(?ms)\[\[source\]\].*?(?=\n\[\[source\]\]|\Z)', patch_block, text)
n = sum(1 for _ in re.finditer(r'(?ms)\[\[source\]\][^\[]*?' + re.escape(new_base) + r'[^\[]*?active\s*=\s*true', new_text))
open(p, "w", encoding="utf-8").write(new_text)
print(f"  total active rows touching {new_base}: {n}")
PY
fi

# Validate TOML
echo
echo "=== validating TOML ==="
uv run python -c "import tomllib; tomllib.loads(open('sources.toml').read()); print('✓ valid')" \
    || { echo "✗ TOML broke. Restoring from backup."; cp sources.toml.bak sources.toml; exit 1; }

echo
echo "Next: uv run radar fetch  # the rewritten Zhihu/X rows should now hit your local instance"
