#!/usr/bin/env bash
# 后台一键跑: prefilter → score(kimi) → weight (不 fetch, 不出 report)
# 日志: data/pipeline.log    用 `tail -f` 看进度
# 用法: nohup bash scripts/run-pipeline-kimi-all.sh > data/pipeline.log 2>&1 < /dev/null &

set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

UV="${UV:-$(command -v uv || echo "$PROJECT_ROOT/.venv/bin/uv")}"
export UV_PROJECT_ENVIRONMENT="$PROJECT_ROOT/.venv"

if ! command -v "$UV" &> /dev/null; then
  echo "Error: uv not found in PATH or $PROJECT_ROOT/.venv/bin/uv" >&2
  exit 1
fi

LIMIT="${LIMIT:-10000}"

echo "=== $(date -Iseconds)  prefilter --limit $LIMIT ==="
"$UV" run radar prefilter --limit "$LIMIT"

echo "=== $(date -Iseconds)  score --backend kimi --limit $LIMIT ==="
"$UV" run radar score --backend kimi --limit "$LIMIT"

echo "=== $(date -Iseconds)  weight ==="
"$UV" run radar weight

echo "=== $(date -Iseconds)  done ==="
