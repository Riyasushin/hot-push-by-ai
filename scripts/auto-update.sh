#!/usr/bin/env bash
# Used for auto fetch filter scoring the resources in a single .sh




set -euo pipefail

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

# 1. 抓取 + 入库 (RSS / WeRead, 不需要 API key)
"$UV" run radar fetch

# 2. 预筛 (kimi-cli, 本地零成本; 先装并配置好 kimi-cli)
"$UV" run radar prefilter --limit 200

# 用 kimi-cli 跑评分 (慢但免费; 内部 thinking 关掉, JSON 提取不需要 CoT)
"$UV" run radar score --limit 30 --backend kimi

# 4. 加权 + 阈值精选 (纯代码, 无 LLM, 改完 weights.toml 任意重跑)
uv run radar weight

# 5. 写当日 Markdown 日报
uv run radar report

