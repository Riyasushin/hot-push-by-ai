#!/usr/bin/env bash
# weread-keepalive.sh — actively refresh wr_skey via the renewal endpoint.
#
# How WeRead's wr_skey actually works (verified 2026-05-07 against the
# production-grade weread-bot at github.com/funnyzak/weread-bot):
#
#   - wr_skey base validity is ~5400s (~90 min) ABSOLUTE, not sliding.
#   - Hitting the homepage GET / does NOT reliably rotate the skey
#     (this contradicts Hank 2022-05; that mechanism appears to be deprecated).
#   - The actual refresh is an explicit POST to a dedicated endpoint:
#         POST https://weread.qq.com/web/login/renewal
#         Content-Type: application/json
#         Body: {"rq": "%2Fweb%2Fbook%2Fread", "ql": false}
#     New wr_skey returns either as a Set-Cookie header or in the JSON body.
#   - The ``ql`` flag is per-account: false works for most users; if a refresh
#     returns no new skey, we automatically retry with ql=true.
#
# Strategy: every interval, POST /web/login/renewal → harvest new skey →
# persist to .env. Each successful refresh resets the 90-min clock.
#
# Output line meanings:
#   ✓ ROTATED wr_skey   = new skey received and written
#   ✓ OK no rotation    = renewal returned without new skey (rare; cookie alive)
#   ✗ EXPIRED           = cookie permanently dead (re-grab via cookie-grab.sh)
#
# Usage:
#   bash scripts/weread-keepalive.sh                     # foreground (in tmux for ssh)
#   bash scripts/weread-keepalive.sh > data/weread.log 2>&1 &
#   bash scripts/weread-keepalive.sh --once             # one ping (cron)
#
# Cron form (every 30 min — well inside 90-min absolute window):
#   */30 * * * * cd /path/to/repo && bash scripts/weread-keepalive.sh --once \
#                                  >> data/weread.log 2>&1

set -uo pipefail

INTERVAL="${WEREAD_KEEPALIVE_INTERVAL:-1800}"   # default 30 min (well within 90-min absolute lifetime)
ENV_FILE="${ENV_FILE:-.env}"
RENEW_URL="https://weread.qq.com/web/login/renewal"
PROBE_URL="https://weread.qq.com/web/shelf/sync"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
REFERER="https://weread.qq.com/"
# Renewal request body. ql=false works for most accounts; auto-retry with true on miss.
RENEW_BODY_QL_FALSE='{"rq":"%2Fweb%2Fbook%2Fread","ql":false}'
RENEW_BODY_QL_TRUE='{"rq":"%2Fweb%2Fbook%2Fread","ql":true}'

read_cookie() {
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "✗ $ENV_FILE not found." >&2
        return 1
    fi
    local line
    line=$(grep -E '^[[:space:]]*(weread_cookie|WEREAD_COOKIE)[[:space:]]*=' "$ENV_FILE" | head -1)
    if [[ -z "$line" ]]; then
        echo "✗ no weread_cookie / WEREAD_COOKIE in $ENV_FILE." >&2
        return 1
    fi
    line="${line#*=}"
    line="${line#"${line%%[![:space:]]*}"}"   # ltrim
    line="${line%\"}"; line="${line#\"}"
    line="${line%\'}"; line="${line#\'}"
    printf '%s' "$line"
}

# Replace wr_skey value in a cookie string. Appends if absent.
swap_skey() {
    local cookie="$1" new="$2"
    if [[ "$cookie" == *"wr_skey="* ]]; then
        # sed -E with explicit BSD/GNU-portable syntax.
        # Use | as delimiter; new value is base64-ish so should never contain |.
        printf '%s' "$cookie" | sed -E "s|wr_skey=[^;]*|wr_skey=${new}|"
    else
        printf '%s; wr_skey=%s' "$cookie" "$new"
    fi
}

# Atomically replace the weread_cookie= line in .env. Falls back to append.
write_back_cookie() {
    local new="$1"
    python3 - "$ENV_FILE" "$new" <<'PYEOF'
import os, pathlib, re, sys, tempfile

env_path, new_cookie = sys.argv[1], sys.argv[2]
p = pathlib.Path(env_path)
text = p.read_text() if p.exists() else ""

pat = re.compile(
    r'^([ \t]*)(weread_cookie|WEREAD_COOKIE)([ \t]*=[ \t]*)(.*)$',
    re.MULTILINE,
)
def repl(m):
    indent, key, eq, _ = m.groups()
    return f'{indent}{key}{eq}"{new_cookie}"'
out, n = pat.subn(repl, text, count=1)
if n == 0:
    out = (text.rstrip("\n") + "\n" if text else "") + f'weread_cookie="{new_cookie}"\n'

# Atomic: write to .env.tmp, rename. Preserves existing file mode if any.
mode = p.stat().st_mode if p.exists() else 0o600
fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".env.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        f.write(out)
    os.chmod(tmp_name, mode)
    os.replace(tmp_name, p)
except Exception:
    os.unlink(tmp_name)
    raise
PYEOF
}

# Extract first Set-Cookie wr_skey value from a curl -D headers file. Empty if none.
# Uses grep+sed because mawk (non-gnu awk) lacks IGNORECASE; HTTP/2 sends
# lowercase ``set-cookie:`` so case-insensitive matching is mandatory.
extract_new_skey() {
    grep -i '^set-cookie:[[:space:]]*wr_skey=' "$1" 2>/dev/null \
        | head -1 \
        | sed -E 's/^[^:]*:[[:space:]]*wr_skey=([^;]*).*/\1/I' \
        | tr -d '\r\n[:space:]'
}

# Try one renewal POST. Stdout: new skey if any. Return: 0 success, non-0 error.
try_renew() {
    local cookie="$1" ql_body="$2" hdr body status
    hdr=$(mktemp); body=$(mktemp)
    status=$(curl -s --max-time 15 -o "$body" -D "$hdr" -w '%{http_code}' \
                -X POST \
                -H "Cookie: $cookie" \
                -H "User-Agent: $UA" \
                -H "Referer: $REFERER" \
                -H "Content-Type: application/json" \
                -H "Accept: application/json" \
                --data "$ql_body" \
                "$RENEW_URL" || echo "000")
    if [[ "$status" != "200" ]]; then
        echo "HTTP_$status" >&2
        rm -f "$hdr" "$body"
        return 3
    fi
    # -2012 in body → cookie genuinely dead
    if grep -q '"errCode"[[:space:]]*:[[:space:]]*-2012\|"errcode"[[:space:]]*:[[:space:]]*-2012' "$body"; then
        rm -f "$hdr" "$body"
        return 4
    fi
    # Try Set-Cookie header first
    local new_skey
    new_skey=$(extract_new_skey "$hdr")
    # Fallback: parse JSON body for a skey field
    if [[ -z "$new_skey" ]]; then
        new_skey=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for k in ('skey','wr_skey','newSkey','new_skey'):
    v = d.get(k) if isinstance(d, dict) else None
    if v:
        print(v); break
" "$body" 2>/dev/null)
    fi
    rm -f "$hdr" "$body"
    if [[ -n "$new_skey" ]]; then
        printf '%s' "$new_skey"
        return 0
    fi
    return 5
}

ping_once() {
    local cookie
    cookie="$(read_cookie)" || return 2

    local now ; now=$(date +'%Y-%m-%d %H:%M:%S')
    local new_skey rc

    # ----- 1. POST /web/login/renewal with ql=false ----------------------
    new_skey=$(try_renew "$cookie" "$RENEW_BODY_QL_FALSE"); rc=$?

    # ----- 2. If no skey returned, retry with ql=true (per-account flag) -
    if [[ -z "$new_skey" && "$rc" -ne 4 ]]; then
        new_skey=$(try_renew "$cookie" "$RENEW_BODY_QL_TRUE"); rc=$?
    fi

    if [[ "$rc" -eq 4 ]]; then
        echo "$now ✗ EXPIRED (errcode -2012). Re-grab via scripts/weread-cookie-grab.sh."
        return 4
    fi
    if [[ "$rc" -eq 3 ]]; then
        echo "$now ✗ HTTP error from $RENEW_URL"
        return 3
    fi

    if [[ -z "$new_skey" ]]; then
        # Both ql variants returned 200 but no skey. Probe shelf to verify alive.
        local body status
        body=$(mktemp)
        status=$(curl -s --max-time 12 -o "$body" -w '%{http_code}' \
                    -H "Cookie: $cookie" -H "User-Agent: $UA" -H "Referer: $REFERER" \
                    "$PROBE_URL" || echo "000")
        if [[ "$status" == "200" ]] && grep -q '"books"' "$body"; then
            echo "$now ⚠ no rotation this time (renewal 200 but no new skey, shelf still works)"
            rm -f "$body"
            return 0
        fi
        rm -f "$body"
        echo "$now ✗ renewal returned no skey + shelf probe failed"
        return 5
    fi

    # ----- 3. Splice new skey into cookie + persist ----------------------
    local new_cookie
    new_cookie="$(swap_skey "$cookie" "$new_skey")"
    if ! write_back_cookie "$new_cookie"; then
        echo "$now ⚠ got new skey but persist to $ENV_FILE failed"
        return 6
    fi
    echo "$now ✓ ROTATED wr_skey (${new_skey:0:8}***, persisted to $ENV_FILE)"
}

if [[ "${1:-}" == "--once" ]]; then
    ping_once
    exit $?
fi

echo "$(date +'%Y-%m-%d %H:%M:%S') keepalive starting (interval=${INTERVAL}s)"
echo "$(date +'%Y-%m-%d %H:%M:%S') strategy: POST /web/login/renewal -> harvest new wr_skey -> persist to $ENV_FILE"
echo "$(date +'%Y-%m-%d %H:%M:%S') self-stops on -2012 (cookie genuinely dead) or 3 consecutive HTTP failures"
trap 'echo "$(date +'\''%Y-%m-%d %H:%M:%S'\'') keepalive stopping (signal)"; exit 0' INT TERM

http_fail_streak=0
MAX_HTTP_FAILS=3

while true; do
    ping_once; rc=$?
    case "$rc" in
        0)
            http_fail_streak=0
            ;;
        4)
            echo "$(date +'%Y-%m-%d %H:%M:%S') ✗✗ STOPPING — cookie expired."
            echo "$(date +'%Y-%m-%d %H:%M:%S')      bash scripts/weread-cookie-grab.sh --write"
            echo "$(date +'%Y-%m-%d %H:%M:%S')      then restart: nohup bash scripts/weread-keepalive.sh > data/weread.log 2>&1 &"
            exit 0
            ;;
        2)
            echo "$(date +'%Y-%m-%d %H:%M:%S') ✗✗ STOPPING — no readable cookie in env (config error)."
            exit 1
            ;;
        3|5|6)
            http_fail_streak=$((http_fail_streak + 1))
            echo "$(date +'%Y-%m-%d %H:%M:%S') (consecutive failures: $http_fail_streak/$MAX_HTTP_FAILS)"
            if (( http_fail_streak >= MAX_HTTP_FAILS )); then
                echo "$(date +'%Y-%m-%d %H:%M:%S') ✗✗ STOPPING — $MAX_HTTP_FAILS consecutive failures."
                exit 0
            fi
            ;;
    esac
    sleep "$INTERVAL"
done
