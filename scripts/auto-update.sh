#!/usr/bin/env bash
# 一把梭: fetch → prefilter → score(kimi) → weight → report
# 适合每天 cron 跑一次. 频繁跑请改用 crontab.example 拆开调度.

set -euo pipefail

# cron 启动时 PATH 极简, 显式补上 uv / kimi-cli 所在目录
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# 1. 切换到项目根目录（脚本位于 scripts/ 下，.. 即根目录）
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

# 2. 确保使用正确目录下的 uv（若系统 PATH 中没有，可写死路径）
UV="${UV:-$(command -v uv || echo "$PROJECT_ROOT/.venv/bin/uv")}"

# 3. 确保 uv 能找到 .venv（uv 默认就会找当前目录的 .venv）
export UV_PROJECT_ENVIRONMENT="$PROJECT_ROOT/.venv"

# 可选：校验 uv 存在
if ! command -v "$UV" &> /dev/null; then
    echo "Error: uv not found. Install it first." >&2
    exit 1
fi

"$UV" sync

# 0. 同步起点阅读进度 (qidian fetcher 依赖 data/qidian_progress.json)
#    必须在 fetch 之前: fetcher 读 progress 决定是否 emit.
#    || true: 没有 active qidian 源 / cookie 失效都不应炸掉整条流水.
#    不走 proxy (qidian 国内站, 7890 反而更慢/会断).
"$UV" run python scripts/qidian-progress-sync.py --once || true

# 1. 抓取 + 入库 (RSS / WeRead, 不需要 API key)
#    走本地代理 7890; 排除 localhost/127.* 让本地 RSSHub (41200) 直连
#    .ts.net / 100.64/10 走 tailscale 直连 (paper-radar 源在自己的 tailnet)
#    代理只覆盖这一步, score 走 kimi-cli (国内) 不能被代理
http_proxy="${PROXY:-http://127.0.0.1:7890}" \
https_proxy="${PROXY:-http://127.0.0.1:7890}" \
no_proxy="localhost,127.0.0.1,::1,.ts.net,100.64.0.0/10" \
"$UV" run radar fetch

# 2. 预筛 (kimi-cli, 本地零成本; 先装并配置好 kimi-cli)
"$UV" run radar prefilter --limit 200

# 用 kimi-cli 跑评分 (慢但免费; 内部 thinking 关掉, JSON 提取不需要 CoT)
"$UV" run radar score --limit 200 --backend kimi

# 4. 加权 + 阈值精选 (纯代码, 无 LLM, 改完 weights.toml 任意重跑)
"$UV" run radar weight

# 5. 写当日 Markdown 日报
"$UV" run radar report

