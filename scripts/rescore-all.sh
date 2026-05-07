#!/usr/bin/env bash
# scripts/rescore-all.sh — 清掉 scores 表全部重打分 + 重 weight，可后台跑。
#
# 触发场景：score prompt / weights.toml 大改后, 想让所有历史条目按新规则重打.
# 不影响 items / fetch_runs / feedback 几张表 → 抓取记录与你的 👍 👎 都不丢.
#
# 行为:
#   1. 互斥锁: 同一脚本不允许并发跑 (避免双倍 LLM 计费)
#   2. DELETE FROM scores                  (一次性, 走 transaction 安全)
#   3. 循环 radar score 直到 pending=0     (kimi-cli 偶发超时由下一轮捡起)
#   4. radar weight                        (按 weights.toml 重算 total/is_selected)
#
# 使用:
#   tmux new -s rescore                                       # 推荐: 后台 + 长跑
#   bash scripts/rescore-all.sh
#   # Ctrl-B d  detach; tmux attach -t rescore  回来看
#
#   # 或不开 tmux 走 nohup:
#   nohup bash scripts/rescore-all.sh > data/rescore.log 2>&1 &
#   tail -f data/rescore.log
#
# 调参 (env var, 都有合理默认):
#   BACKEND=kimi          (默认; 走 kimi-cli, 免费; 慢)
#   BACKEND=deepseek      (要 DEEPSEEK_API_KEY; 快但花钱)
#   BATCH_SIZE=3          (默认 3; kimi 容易超时, 小批次稳)
#   LIMIT_PER_RUN=30      (每轮 score 跑多少条; 控制单进程时长)

set -uo pipefail
cd "$(dirname "$0")/.."

BACKEND="${BACKEND:-kimi}"
BATCH_SIZE="${BATCH_SIZE:-3}"
LIMIT_PER_RUN="${LIMIT_PER_RUN:-30}"
DB="data/radar.db"
LOCK="data/.rescore.lock"

# ----- 互斥 -----
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "✗ 另一个 rescore-all.sh 还在跑 (锁文件: $LOCK). 退出."
    exit 1
fi

# ----- 前置检查 -----
if [[ ! -f "$DB" ]]; then
    echo "✗ DB 不存在: $DB. 先跑 radar fetch."
    exit 1
fi

if [[ "$BACKEND" == "deepseek" && -z "${DEEPSEEK_API_KEY:-${DPSK_API:-}}" ]]; then
    echo "✗ BACKEND=deepseek 但环境里没 DEEPSEEK_API_KEY / DPSK_API"
    exit 1
fi

START_TS=$(date '+%s')
START_HUMAN=$(date '+%Y-%m-%d %H:%M:%S')

echo "==============================================================="
echo " rescore-all 开始: $START_HUMAN"
echo "  backend       = $BACKEND"
echo "  batch_size    = $BATCH_SIZE"
echo "  limit_per_run = $LIMIT_PER_RUN"
echo "==============================================================="

# ----- Step 1: 清 scores -----
N_BEFORE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM scores")
echo "$(date '+%H:%M:%S') 当前 scores 行数: $N_BEFORE → 全清"
sqlite3 "$DB" "DELETE FROM scores"

# 健全性: 拿到要重打分的 pool 大小 (is_ai_related=1 的条目)
POOL=$(sqlite3 "$DB" "SELECT COUNT(*) FROM items WHERE is_ai_related = 1")
echo "$(date '+%H:%M:%S') 待重打分池: $POOL 条 (is_ai_related=1)"

# ----- Step 2: 循环 score 直到 pending=0 -----
ITER=0
while :; do
    ITER=$((ITER + 1))
    PENDING=$(sqlite3 "$DB" "
        SELECT COUNT(*) FROM items i
        LEFT JOIN scores sc ON sc.item_id = i.id
        WHERE i.is_ai_related = 1 AND sc.item_id IS NULL
    ")
    SCORED=$((POOL - PENDING))
    PCT=$(( POOL > 0 ? SCORED * 100 / POOL : 100 ))
    echo "$(date '+%H:%M:%S') ─ iter $ITER · pending=$PENDING · scored=$SCORED/$POOL ($PCT%)"

    if (( PENDING <= 0 )); then
        echo "$(date '+%H:%M:%S') ✓ 全部打完"
        break
    fi

    # 单轮 score; 失败不让脚本退出, 让下一轮捡漏
    if ! uv run radar score \
            --limit "$LIMIT_PER_RUN" \
            --batch-size "$BATCH_SIZE" \
            --backend "$BACKEND"; then
        echo "$(date '+%H:%M:%S') ⚠ iter $ITER 非 0 退出, 5s 后下一轮"
        sleep 5
    fi

    # 防退化: 如果 pending 完全没动, 说明每次都全失败, 放弃
    NEW_PENDING=$(sqlite3 "$DB" "
        SELECT COUNT(*) FROM items i
        LEFT JOIN scores sc ON sc.item_id = i.id
        WHERE i.is_ai_related = 1 AND sc.item_id IS NULL
    ")
    if (( NEW_PENDING >= PENDING )); then
        STALL=$((${STALL:-0} + 1))
        if (( STALL >= 3 )); then
            echo "$(date '+%H:%M:%S') ✗ pending 连续 3 轮没下降, 放弃. 检查日志."
            exit 2
        fi
    else
        STALL=0
    fi
done

# ----- Step 3: weight -----
echo "$(date '+%H:%M:%S') ── 跑 radar weight 重算 total/is_selected"
uv run radar weight

# ----- 总结 -----
END_TS=$(date '+%s')
ELAPSED=$((END_TS - START_TS))
N_AFTER=$(sqlite3 "$DB" "SELECT COUNT(*) FROM scores")
N_SELECTED=$(sqlite3 "$DB" "SELECT COUNT(*) FROM scores WHERE is_selected = 1")

echo "==============================================================="
echo " rescore-all 完成: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  耗时          = ${ELAPSED}s ($((ELAPSED / 60))m)"
echo "  scores 行     = $N_BEFORE → $N_AFTER"
echo "  selected      = $N_SELECTED"
echo "  iter          = $ITER"
echo "==============================================================="
