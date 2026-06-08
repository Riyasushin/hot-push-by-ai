#!/usr/bin/env bash
# launch.sh — 把 ai-reader 这套 (含依赖) 一次性拉起 / 验证。
#
# 涉及的进程拼图 (画清楚, 别忘):
#
#   ┌────────────────────────── Tailscale (system) ───────────────────────────┐
#   │   tailscaled.service        长跑, 提供 100.x.x.x 网卡 + tailnet 路由       │
#   │   tailscale serve (持久)    /  → 8090   /aihot → 8090/aihot              │
#   │                             /sys-papers → 8011                           │
#   └──────────────────────────────────────────────────────────────────────────┘
#                ↓ HTTPS, tailnet-only
#   ┌────────────────────────── 反代 + 后端 ────────────────────────────────────┐
#   │   caddy.service     (user)  127.0.0.1:8090 反代到下面三块                 │
#   │   aihot-reader.service (user) → 127.0.0.1:8000 (radar serve, /aihot)     │
#   │   paper-radar.service (system) → 127.0.0.1:8011  (paper-radar serve)     │
#   │   neudrive (略 — 走自家 systemd unit)                                     │
#   └──────────────────────────────────────────────────────────────────────────┘
#                ↑                          ↑
#                │                          │ fetcher RSS
#   ┌────────────┴──────────────────────────┴─────────────────────────────────┐
#   │   docker: ai-radar-rsshub (1200 → 127.0.0.1:41200)  RSSHub 镜像          │
#   │   docker: ai-radar-rsshub-redis                       redis (RSSHub 缓存) │
#   │   weread-keepalive.service (user)  半小时 POST /web/login/renewal        │
#   └──────────────────────────────────────────────────────────────────────────┘
#                ↑
#   ┌────────────┴──────────────────────────────────────────────────────────────┐
#   │   crontab (用户级):                                                       │
#   │   - 04:00 / 16:00 CST: scripts/auto-update.sh (fetch→prefilter→…→report) │
#   │   - 10:00 CST:        paper-radar enrich --limit 6                       │
#   │   - 15:00 CST:        paper-radar tick                                   │
#   │   - 05:00 周日:       paper-radar backfill-pdf --scope planned           │
#   └──────────────────────────────────────────────────────────────────────────┘
#
# Usage:
#   bash scripts/launch.sh             # 拉起所有依赖 + 烟测
#   bash scripts/launch.sh --check     # 只烟测, 不重启
#   bash scripts/launch.sh --restart   # 强制 restart 而不是 start
#   bash scripts/launch.sh --bootstrap # 烟测 + 自动补缺失的 cron 行
#
# 退出码: 任一关键服务起不来 → exit 1; 全 OK → exit 0.

set -uo pipefail

MODE="up"
case "${1:-}" in
  --check)     MODE="check" ;;
  --restart)   MODE="restart" ;;
  --bootstrap) MODE="bootstrap" ;;
  --help|-h)   sed -n '2,42p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

ok=0; fail=0; warn=0
say()   { printf '[launch] %s\n' "$*"; }
good()  { printf '[launch] \033[32m✓\033[0m %s\n' "$*"; ok=$((ok+1)); }
bad()   { printf '[launch] \033[31m✗\033[0m %s\n' "$*"; fail=$((fail+1)); }
note()  { printf '[launch] \033[33m·\033[0m %s\n' "$*"; warn=$((warn+1)); }

# --- 0. tailscaled (system, 没起就提示用户手动 sudo) -----------------------------
say "0/6 检查 tailscaled (system)"
if systemctl is-active --quiet tailscaled.service; then
  good "tailscaled.service active"
else
  if [[ "$MODE" != "check" ]]; then
    note "tailscaled 没起 — 需要 sudo, 请手动: sudo systemctl start tailscaled.service"
  else
    bad "tailscaled.service inactive"
  fi
fi
if tailscale status >/dev/null 2>&1; then
  good "tailscale up (`tailscale status --self --json 2>/dev/null | grep -o '"DNSName":"[^"]*"' | head -1`)"
else
  note "tailscale 未登录或网络未就绪 — 运行 'tailscale up' 后重试"
fi

# --- 1. Docker 容器 (RSSHub + redis) -------------------------------------------
say "1/6 检查 docker 容器 (RSSHub)"
if ! command -v docker >/dev/null 2>&1; then
  bad "docker CLI 不存在"
elif ! docker info >/dev/null 2>&1; then
  bad "docker daemon 未运行 — sudo systemctl start docker"
else
  for c in ai-radar-rsshub-redis ai-radar-rsshub; do
    state=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo "missing")
    case "$state" in
      running)
        if [[ "$MODE" == "restart" ]]; then
          docker restart "$c" >/dev/null && good "$c restarted" || bad "$c restart failed"
        else
          good "$c running"
        fi ;;
      exited|created|paused)
        [[ "$MODE" != "check" ]] && docker start "$c" >/dev/null \
          && good "$c started" || bad "$c (state=$state) 起不来"
        ;;
      missing) bad "容器 $c 不存在 — 跑 deploy 重建 (镜像: diygod/rsshub:chromium-bundled)" ;;
      *)       bad "$c state=$state" ;;
    esac
  done
  # 快速烟测 RSSHub
  if curl -s --max-time 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:41200/ | grep -qE '^(200|404)$'; then
    good "RSSHub 端口 41200 响应"
  else
    bad "RSSHub 127.0.0.1:41200 不响应"
  fi
fi

# --- 2. paper-radar (system service) -------------------------------------------
say "2/6 检查 paper-radar.service (system)"
verb="start"; [[ "$MODE" == "restart" ]] && verb="restart"
if [[ "$MODE" != "check" ]]; then
  if systemctl is-active --quiet paper-radar.service; then
    [[ "$verb" == "restart" ]] && sudo -n systemctl restart paper-radar.service 2>/dev/null \
      && good "paper-radar restarted" || good "paper-radar already active"
  else
    sudo -n systemctl start paper-radar.service 2>/dev/null \
      && good "paper-radar started" \
      || bad "paper-radar 启动失败 (需要 sudo): sudo systemctl start paper-radar.service"
  fi
else
  systemctl is-active --quiet paper-radar.service \
    && good "paper-radar active" || bad "paper-radar inactive"
fi
code=$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8011/feed/papers.xml)
[[ "$code" == "200" ]] && good "paper-radar /feed/papers.xml=200" || bad "paper-radar /feed=$code"

# --- 3. ai-reader 本体 (aihot-reader.service, user) ----------------------------
say "3/6 检查 aihot-reader.service (user)"
if [[ "$MODE" != "check" ]]; then
  if systemctl --user is-active --quiet aihot-reader.service; then
    [[ "$MODE" == "restart" ]] && systemctl --user restart aihot-reader.service \
      && good "aihot-reader restarted" || good "aihot-reader already active"
  else
    systemctl --user start aihot-reader.service \
      && good "aihot-reader started" || bad "aihot-reader 启动失败"
  fi
else
  systemctl --user is-active --quiet aihot-reader.service \
    && good "aihot-reader active" || bad "aihot-reader inactive"
fi
# 烟测内层 (FastAPI --root-path /aihot 只影响 URL 生成, 实际路由仍挂在 /)
code=$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/)
[[ "$code" == "200" ]] && good "aihot-reader 127.0.0.1:8000/=200" || bad "aihot-reader 内层=$code"

# --- 4. Caddy 反代 (user) -------------------------------------------------------
say "4/6 检查 caddy.service (user)"
if [[ "$MODE" != "check" ]]; then
  if systemctl --user is-active --quiet caddy.service; then
    [[ "$MODE" == "restart" ]] && systemctl --user restart caddy.service \
      && good "caddy restarted" || good "caddy already active"
  else
    systemctl --user start caddy.service \
      && good "caddy started" || bad "caddy 启动失败"
  fi
else
  systemctl --user is-active --quiet caddy.service \
    && good "caddy active" || bad "caddy inactive"
fi
code=$(curl -s --max-time 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/aihot/)
[[ "$code" == "200" ]] && good "caddy /aihot 反代 =200" || bad "caddy /aihot=$code"

# --- 5. weread-keepalive (user) ------------------------------------------------
say "5/6 检查 weread-keepalive.service (user)"
if [[ "$MODE" != "check" ]]; then
  systemctl --user is-active --quiet weread-keepalive.service \
    || systemctl --user start weread-keepalive.service 2>/dev/null
fi
if systemctl --user is-active --quiet weread-keepalive.service; then
  good "weread-keepalive active"
else
  note "weread-keepalive inactive — cookie 可能已死, 重新抓: bash scripts/weread-cookie-update.sh"
fi

# --- 6. cron 完整性 -------------------------------------------------------------
say "6/6 检查用户级 cron"
cron=$(crontab -l 2>/dev/null || true)
# 期望 crontab 行 (按 key 索引, 缺失时用对应 line 补)
declare -A CRON_LINES=(
  [auto-update.sh]='0 4,16 * * * /home/rj/Apps/ai-reader/scripts/auto-update.sh >> /home/rj/Apps/ai-reader/data/auto-update.log 2>&1'
  [enrich]='0 10 * * * cd /home/rj/Apps/paper-radar && /home/rj/.local/bin/uv run paper-radar enrich --limit 6 >> data/enrich.log 2>&1'
  [tick]='0 15 * * * cd /home/rj/Apps/paper-radar && /home/rj/.local/bin/uv run paper-radar tick >> data/tick.log 2>&1'
  [backfill]='0 5 * * 0 cd /home/rj/Apps/paper-radar && /home/rj/.local/bin/uv run paper-radar backfill-pdf --scope planned >> data/backfill.log 2>&1'
)
declare -A CRON_PATTERNS=(
  [auto-update.sh]="auto-update.sh"
  [enrich]="paper-radar enrich"
  [tick]="paper-radar tick"
  [backfill]="paper-radar backfill-pdf"
)
missing_keys=()
for k in auto-update.sh enrich tick backfill; do
  if echo "$cron" | grep -qF "${CRON_PATTERNS[$k]}"; then
    good "cron 含 '${CRON_PATTERNS[$k]}'"
  else
    if [[ "$MODE" == "bootstrap" ]]; then
      missing_keys+=("$k")
      note "cron 缺 '${CRON_PATTERNS[$k]}' — bootstrap 将补"
    else
      bad "cron 缺 '${CRON_PATTERNS[$k]}' (用 --bootstrap 自动补, 或手抄 paper-radar/crontab.example)"
    fi
  fi
done

if [[ "$MODE" == "bootstrap" && ${#missing_keys[@]} -gt 0 ]]; then
  say "bootstrap: 写入 ${#missing_keys[@]} 行 cron"
  new_cron="$cron"
  for k in "${missing_keys[@]}"; do
    new_cron="${new_cron}"$'\n'"${CRON_LINES[$k]}"
  done
  if echo "$new_cron" | crontab -; then
    good "crontab 已更新 (+${#missing_keys[@]})"
  else
    bad "crontab 写入失败"
  fi
fi

# --- 烟测 tailnet (可选, 走 https) ----------------------------------------------
if command -v curl >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
  for url in \
    "https://rijoshin-omen-1.tail88a62f.ts.net/aihot/" \
    "https://rijoshin-omen-1.tail88a62f.ts.net/sys-papers/feed/papers.xml"; do
    code=$(curl -s --noproxy '*' --max-time 5 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo "0")
    [[ "$code" == "200" ]] && good "tailnet $url =200" || note "tailnet $url =$code"
  done
fi

echo
say "总结: ok=$ok  fail=$fail  warn=$warn"
exit $(( fail > 0 ? 1 : 0 ))
