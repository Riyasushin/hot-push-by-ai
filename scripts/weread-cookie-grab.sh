#!/usr/bin/env bash
# weread-cookie-grab.sh — Mac helper for refreshing the WeRead cookie.
#
# Workflow:
#   1. Open https://weread.qq.com/ in your browser (logged in).
#   2. DevTools → Network → click any request to weread.qq.com →
#      right-click → Copy → Copy as cURL.
#   3. Run this script. It uses Python to parse the curl carefully —
#      survives multi-line curl output, mixed quoting, and avoids picking
#      the wrong "cookie" substring.
#        Default: writes  weread_cookie="..."  (full .env line) to clipboard.
#        --write: rewrite the project's .env in place (backup at .env.bak).
#        --debug: dump clipboard to /tmp/weread-clip.txt + per-step parse trace.

set -uo pipefail

DEBUG=0
WRITE=0
for arg in "$@"; do
    case "$arg" in
        --debug) DEBUG=1 ;;
        --write) WRITE=1 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "✗ unknown arg: $arg (try --help)" >&2
            exit 1
            ;;
    esac
done

log()  { echo "  $*"; }
ok()   { echo "✓ $*"; }
warn() { echo "⚠ $*" >&2; }
fail() { echo "✗ $*" >&2; exit 1; }

# --------------------------------------------------------------- platform --

if ! command -v pbpaste >/dev/null || ! command -v pbcopy >/dev/null; then
    fail "pbpaste / pbcopy not found — this script is macOS-only."
fi
command -v python3 >/dev/null || fail "python3 not found (Mac usually has it; try \`brew install python3\` or activate your conda env)."

# --------------------------------------------------------------- clipboard --

log "Reading clipboard…"
PASTE="$(pbpaste)"
PASTE_LEN=${#PASTE}

if (( PASTE_LEN == 0 )); then
    fail "Clipboard is empty.
  → DevTools → Network → right-click a weread.qq.com request → Copy → Copy as cURL,
    then re-run this script."
fi
ok "Clipboard has $PASTE_LEN chars."

if (( DEBUG )); then
    DUMP="/tmp/weread-clip.txt"
    printf '%s' "$PASTE" > "$DUMP"
    log "[debug] dumped clipboard → $DUMP (inspect with: less $DUMP)"
    log "[debug] first 240 chars:"
    printf '    %s\n' "${PASTE:0:240}" | head -c 600
    echo
fi

# --------------------------------------------------------------- extract --

log "Parsing for Cookie header…"

# Hand off to Python: regex with multiline + case-insensitive, prefer the
# longest candidate that contains '=' signs (real cookies look like a=1; b=2…).
# Searches three syntaxes:
#   1. -H 'cookie: …'  /  -H "cookie: …"
#   2. --header 'cookie: …' / "…"
#   3. -b '…'  /  --cookie '…'
#
# Also handles bash-style $'…' (ANSI-C quoting Chrome uses for special chars).
COOKIE="$(DEBUG=$DEBUG python3 - <<'PY'
import os, re, sys, subprocess
text = subprocess.check_output(['pbpaste']).decode('utf-8', errors='replace')
debug = os.environ.get('DEBUG') == '1'
def d(*a):
    if debug: print('[debug-py]', *a, file=sys.stderr)

# Strip line continuations so multi-line -H values aren't a problem.
flat = text.replace('\\\n', ' ')

candidates = []  # (source, value)

# 1) -H / --header forms with single, double, or $'' quoting
patterns = [
    # -H 'cookie: …'
    (r"(?:-H|--header)\s+\$?'((?:[^'\\]|\\.)*?)'", 'single'),
    # -H "cookie: …"
    (r'(?:-H|--header)\s+"((?:[^"\\]|\\.)*?)"', 'double'),
]
for pat, label in patterns:
    for m in re.finditer(pat, flat, re.IGNORECASE):
        body = m.group(1)
        # Strip optional 'cookie: ' / 'Cookie: ' prefix
        sub = re.match(r'\s*[Cc]ookie\s*:\s*(.+)$', body, re.DOTALL)
        if sub:
            v = sub.group(1).strip()
            if '=' in v:
                candidates.append((f'-H {label}', v))
                d(f'header {label}: matched {len(v)} chars, {v.count("=")} =')

# 2) -b / --cookie form
for pat in [r"-b\s+\$?'((?:[^'\\]|\\.)*?)'",
            r'-b\s+"((?:[^"\\]|\\.)*?)"',
            r'--cookie\s+\$?\'((?:[^\'\\\\]|\\\\.)*?)\'',
            r'--cookie\s+"((?:[^"\\\\]|\\\\.)*?)"']:
    for m in re.finditer(pat, flat, re.IGNORECASE):
        v = m.group(1).strip()
        # Strip optional 'cookie:' prefix (rare for -b but defensive)
        v = re.sub(r'^\s*[Cc]ookie\s*:\s*', '', v)
        if '=' in v and ('wr_' in v or 'session' in v.lower() or len(v) > 100):
            candidates.append(('-b/--cookie', v))
            d(f'-b: matched {len(v)} chars, {v.count("=")} =')

# 3) raw cookie paste (no curl wrapping)
if not candidates and 'wr_vid=' in text and 'wr_skey=' in text:
    line = next((ln for ln in text.splitlines() if ln.strip()), '')
    line = re.sub(r'^\s*[Cc]ookie\s*:\s*', '', line.strip())
    line = line.strip("'\"")
    if '=' in line:
        candidates.append(('raw-paste', line))

# Unescape any \' or \" the shell would have eaten
def unesc(s):
    return s.replace("\\'", "'").replace('\\"', '"').replace('\\\\', '\\')

candidates = [(src, unesc(v)) for src, v in candidates]

if debug:
    for src, v in candidates:
        keys = re.findall(r'(?:^|;\s*)([^=;\s]+)\s*=', v)
        d(f'candidate from {src}: {len(v)}b, {v.count("=")} keys, sample: {keys[:5]}…')

if not candidates:
    sys.exit(0)  # empty stdout signals "not found"

# Pick the candidate with the most '=' signs (most cookie items).
best_src, best = max(candidates, key=lambda c: c[1].count('='))
print(best)
PY
)"

if [[ -z "$COOKIE" ]]; then
    echo
    warn "Could not extract a cookie from clipboard."
    cat >&2 <<EOM

What I expected on the clipboard:

  (a) curl as bash — DevTools → Network → request → right-click → Copy → Copy as cURL:
      curl 'https://weread.qq.com/web/...' \\
        -H 'cookie: _qimei_h38=...; wr_vid=...; wr_skey=...' \\
        ...

  (b) raw Cookie line from Request Headers panel:
      _qimei_h38=...; wr_vid=...; wr_skey=...

What likely happened:
  • You copied the wrong thing (e.g. the bash command itself, like in run 2).
  • You used "Copy as cURL (cmd)" / "Copy as cURL (PowerShell)" — they have
    different escaping. On Mac Chrome, default is bash-style which is fine.
  • You copied a request to mp.weixin / rescdn / cdn — those don't carry
    the WeRead session. Use a request whose URL is weread.qq.com.

Try again:
  1. Make sure you're on https://weread.qq.com/ and signed in.
  2. DevTools → Network. Reload page if list is empty.
  3. Click ANY row whose URL starts with weread.qq.com.
  4. Right-click → Copy → Copy as cURL (NOT as PowerShell).
  5. Run: bash $(basename "$0") --debug
     (--debug dumps your clipboard to /tmp/weread-clip.txt for inspection)
EOM
    exit 1
fi
ok "Cookie extracted ($(printf '%s' "$COOKIE" | wc -c | tr -d ' ') chars)."

# ------------------------------------------------------------- validate --

# Drop trailing whitespace + stray quote chars
COOKIE="${COOKIE%[\"\']}"
COOKIE="${COOKIE#[\"\']}"
COOKIE="$(printf '%s' "$COOKIE" | sed -E 's/[[:space:]]+$//')"

COUNT=$(printf '%s' "$COOKIE" | tr ';' '\n' | grep -c '=')
log "Cookie has $COUNT items."

KEYS=$(printf '%s' "$COOKIE" | tr ';' '\n' | sed -E 's/[[:space:]]*([^=]+)=.*/\1/' \
       | grep -v '^$' | sort -u | tr '\n' ' ')
(( DEBUG )) && log "[debug] keys: $KEYS"

MISSING=()
for k in wr_vid wr_skey; do
    [[ "$COOKIE" != *"${k}="* ]] && MISSING+=("$k")
done

if (( ${#MISSING[@]} > 0 )); then
    echo
    warn "Cookie is missing required HTTP-only token(s): ${MISSING[*]}"
    cat >&2 <<EOM

You copied something that didn't carry the auth tokens. Common causes:
  • \`copy(document.cookie)\` in JS console — JS can't see HTTP-only.
  • Copied a request to a CDN / static asset — no auth attached.

Items found in your paste: $KEYS

Re-grab from a real weread.qq.com API request and try again.
EOM
    exit 1
fi
ok "wr_vid + wr_skey present."

# Show summary
echo
log "Summary:"
log "  items: $COUNT"
log "  keys:  $KEYS"
log "  size:  $(printf '%s' "$COOKIE" | wc -c | tr -d ' ') chars"

# --------------------------------------------------------------- write out --

LINE='weread_cookie="'"$COOKIE"'"'

echo
if (( WRITE )); then
    ENV_FILE="${ENV_FILE:-.env}"
    [[ -f "$ENV_FILE" ]] || fail ".env not found at $ENV_FILE (set ENV_FILE=path or run from project root)."

    cp "$ENV_FILE" "${ENV_FILE}.bak"
    if grep -q '^weread_cookie=' "$ENV_FILE"; then
        awk -v new="$LINE" '/^weread_cookie=/{print new; next}{print}' \
            "$ENV_FILE" > "${ENV_FILE}.tmp"
        mv "${ENV_FILE}.tmp" "$ENV_FILE"
        ok "Updated $ENV_FILE (backup at ${ENV_FILE}.bak)."
    else
        echo "$LINE" >> "$ENV_FILE"
        ok "Appended to $ENV_FILE (backup at ${ENV_FILE}.bak)."
    fi
    echo
    log "Test: bash scripts/weread-keepalive.sh --once   # should print ✓ OK"
else
    printf '%s' "$LINE" | pbcopy
    ok "Copied weread_cookie=\"…\" to clipboard."
    echo
    log "Now: open .env, replace the existing 'weread_cookie=' line with paste (⌘V)."
    log "Or re-run with --write to rewrite .env automatically."
fi
