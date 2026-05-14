# ai-radar

为一名 AI infra 研究者本人定制的 AI/经济信息聚合器。卡兹克 [AIHOT](https://aihot.virxact.com) 的个人化复线：保留分级信源 + 多维评分 + AI 日报这套架构，但把"内容创作者视角"翻成"研究者视角"——**硬核论文加权而非降权**。



## 文档

- [`docs/DESIGN.md`](./docs/DESIGN.md) —— 详细架构、两条架构铁律、五个核心机制、文件树
- [`docs/ADDING_SOURCES.md`](./docs/ADDING_SOURCES.md) —— 怎么加各种信源（RSS / RSSHub / 公众号 4 条路径 / 微信读书 cookie / 起点小说 qidian fetcher）

## 快速开始

```bash
uv sync

# 1. 抓取 + 入库 (RSS / WeRead, 不需要 API key)
uv run radar fetch
# 只抓单个源 (子串匹配, 忽略 active flag, 用于调试一个具体源):
uv run radar fetch --source paper-radar

# 2. 预筛 (kimi-cli, 本地零成本; 先装并配置好 kimi-cli)
uv run radar prefilter --limit 200

# 3. 评分 (DeepSeek 默认 deepseek-v4-flash; 需要 .env 里有 DEEPSEEK_API_KEY 或 DPSK_API)
#    要评分质量更优可在 .env 里 DEEPSEEK_MODEL=deepseek-v4-pro
uv run radar score --limit 30
# 或者用 kimi-cli 跑评分 (慢但免费; 内部 thinking 关掉, JSON 提取不需要 CoT)
uv run radar score --limit 30 --backend kimi

# 4. 加权 + 阈值精选 (纯代码, 无 LLM, 改完 weights.toml 任意重跑)
uv run radar weight

# 5. 写当日 Markdown 日报
uv run radar report

# 6. 启 Web (默认 127.0.0.1:8000, 也可 --host 0.0.0.0 --port 18086)
uv run radar serve

# 状态查询
uv run radar status            # DB 总条数 / 评分进度 / 各 category 数
uv run radar sources           # 列所有 active 源 + last_fetched
uv run radar weread-list       # 看微信读书的 shelf (需 WEREAD_COOKIE)
```

挂 cron 见 [`crontab.example`](./crontab.example)。

## 一键拉起 / 健康检查

`scripts/launch.sh` 把 ai-reader 及所有依赖 (Tailscale / RSSHub docker / paper-radar / Caddy / WeRead keepalive) 一次性烟测 + 拉起。

```bash
bash scripts/launch.sh             # 检查 + 起所有依赖 + 烟测 (18 个 check, 全绿才退 0)
bash scripts/launch.sh --check     # 只读模式: 不启动 / 不重启, 只看现状
bash scripts/launch.sh --restart   # 全部 restart (不只 start)
bash scripts/launch.sh --bootstrap # 烟测 + 自动补缺失的 cron 行 (auto-update / paper-radar enrich/tick/backfill-pdf)
```

涉及到的服务全图: tailscaled (system) · ai-radar-rsshub + redis (docker) · paper-radar.service (system) · aihot-reader.service + caddy.service + weread-keepalive.service (user-level) · 用户 cron。详见脚本顶部的注释画的 ASCII 拓扑图。

## WeRead cookie 维护（仅当用 weread fetcher）

WeRead 的 `wr_skey` 90 分钟绝对到期。Tencent **不会**因为你 GET 主页就自动续期（Hank 2022 那篇博客的机制现已无效）。
真正的续期端点是 `POST https://weread.qq.com/web/login/renewal`，body `{"rq":"%2Fweb%2Fbook%2Fread","ql":false}`，
服务端用 `wr_rt` (refresh token, 1 年有效) 鉴权，发新 `wr_skey` 到 Set-Cookie。
`scripts/weread-keepalive.sh` v2 就是这套逻辑，每 30 分钟显式 POST renewal。

```bash
# 1. 浏览器登录 https://weread.qq.com/
# 2. F12 → Network → 任选一个 weread.qq.com 请求 → Headers → 复制 Cookie 整行
# 3. 写入 .env:
#       weread_cookie="<整段 Cookie>"

# 验一下 — 应看到 ✓ ROTATED:
bash scripts/weread-keepalive.sh --once
```

**SSH 长跑推荐 tmux**（脚本只在 cookie 真死 (-2012) 或连续 3 次 HTTP 错误时自停, 不会留僵尸进程）:

```bash
tmux new -s weread
bash scripts/weread-keepalive.sh
# Ctrl-B d  detach; 重新 ssh 后 tmux attach -t weread 继续看
```

或者本机 nohup:

```bash
nohup bash scripts/weread-keepalive.sh > data/weread.log 2>&1 &
tail -f data/weread.log
```

**自动停的条件**:
- `errCode -2012` cookie 过期 → 立刻退, log 写明 + 重启命令
- 连续 3 次 HTTP 失败 → 退（网络/端点异常, 不空转）
- Ctrl-C / SIGTERM → 优雅退

跟一天日志看 OK ↔ EXPIRED 模式：连续 ✓ OK 数小时 → sliding-window 假说成立, 这条路通了；规律性 EXPIRED → 绝对到期, 老老实实重抓 cookie 或上 WeWe RSS。

## 环境变量

可以放在 shell env 或项目根的 `.env`（`KEY=value` 或 `export KEY=value` 都支持）。`.env` 已被 gitignore。

| 变量                               | 必填                    | 默认值                     | 用途                                                                                                                                              |
| ---------------------------------- | ----------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DEEPSEEK_API_KEY` 或 `DPSK_API`   | ✅ 评分用                | —                          | DeepSeek 评分 (`pipeline/score.py`)                                                                                                               |
| `DEEPSEEK_API_BASE`                | –                       | `https://api.deepseek.com` | OpenAI-兼容 endpoint                                                                                                                              |
| `DEEPSEEK_MODEL`                   | –                       | `deepseek-v4-flash`        | 模型 ID。V4 系: `deepseek-v4-flash`(默认, 便宜/快)/`deepseek-v4-pro`(更强, 评分质量优先时用)。`deepseek-chat`/`deepseek-reasoner` 2026-07-24 弃用 |
| `WEREAD_COOKIE` 或 `weread_cookie` | 公众号 走 WeRead 时必填 | —                          | 微信读书 cookie；DevTools Network tab → 任一请求 → Request Headers → Cookie 整行复制（不要 `copy(document.cookie)`，会缺 HTTP-only 字段）         |
| `QIDIAN_COOKIE`                    | 订起点小说时必填        | —                          | 起点 cookie；浏览器登录 qidian.com → DevTools → Application → Cookies → qidian.com 全部字段拼成 `k=v; k=v; ...` 一行。供 `scripts/qidian-progress-sync.py` 抓 bookcase HTML 用 |

## 配置文件

只有两个用户面向的 toml：

| 文件           | 调什么                                             | 何时生效            |
| -------------- | -------------------------------------------------- | ------------------- |
| `sources.toml` | 信源（增/删/改、active 开关、tier 调整、URL 替换） | 下次 fetch          |
| `weights.toml` | 4 维权重 + tier 乘子 + 二维阈值                    | 下次 `radar weight` |

两个文件改完都不需要重启服务，下次对应命令即生效。

## 实现状态（2026-05-07）

| Step  | 内容                                                                                                                                                                                         | 状态   |
| ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| 1     | RSS 抓取 + SQLite 入库 + 去重 + cron + 22 英文源                                                                                                                                             | ✅      |
| 2.a   | kimi-cli 预筛                                                                                                                                                                                | ✅      |
| 2.b   | DeepSeek 4 维评分 + 类别 + 中文摘要 + 推荐理由                                                                                                                                               | ✅      |
| ~~3~~ | ~~embedding + 聚类 + relevance_to_me~~                                                                                                                                                       | ❌ 砍掉 |
| 3.5   | 加权（按 weights.toml）+ 二维阈值精选                                                                                                                                                        | ✅      |
| 4     | FastAPI Web 时间线 + Markdown 日报 + 反馈                                                                                                                                                    | ✅      |
| 5.a   | 公众号接入：wechat2rss 14 + WeRead 直连 5 + 官方 RSS 1 = **20 个公众号每日抓取**；WeRead 5 个 (NeuralTalk / PaperAgent / 工程芯一 / 青稞AI / AI Infra之道) 是 wechat2rss **不收录**的硬核源  | ✅      |
| 5.b   | WeRead cookie 自治续期：`scripts/weread-keepalive.sh` 走 `POST /web/login/renewal` 真续期 (不是 GET / 那种过时玩法), 过 90 分钟服务端自动发新 `wr_skey` 写回 .env, **不 ssh 上去也能跑过夜** | ✅      |
| 5.c   | X (RSSHub 自建) ✅ / Zhihu (RSSHub 自建 + 用户 cookie) ✅ / arXiv 摘要专用 / Anthropic scrape                                                                                                  | 🟡 部分 |
| 5.d   | 起点小说订阅：自实现 fetcher (零网络 I/O, 纯读 progress.json) + bookcase 同步脚本 (cookie 抓 my.qidian.com/bookcase HTML, 一次拿 latest+last_read); "领先 N 章才推一次, 只显示书名" 语义       | ✅      |
| 6     | WeWe RSS 自建 / 趋势预测 / 热度指数                                                                                                                                                          | ⏳      |
