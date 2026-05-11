# ai-radar 设计文档

> 长期文档，跟着代码走，与 README/ADDING_SOURCES 三足。
> 灵感来源：卡兹克 [AIHOT](https://aihot.virxact.com)（[原文存档](../这个封装了我3年自媒体经验的AI热点网站，今天向所有人免费开放。.webarchive)）。
> 上次校准：2026-05-07

## 这是什么

为我本人定制的 AI/经济信息聚合器。

面对过量的信息，我的注意力是有限的，我能做的事情也是有限的，这是一个为了我打造的，对我感兴趣的信息进行过滤的网页，会包括
- AI 技术的发展(Infra, Model, Training, Inference, Evaluation......)
- AI 经济社会的发展。有哪些有意思的、好玩的AI产品，有哪些相关的经济、社会的事件，有哪些相关的历史、哲学的讨论


> ⚠️ **设计偏离 AIHOT**：本项目**砍掉了** AIHOT 用的 embedding-based "relevance_to_me" 维度和事件聚类。
> 用户决策（2026-05-07）：**taste 通过信源筛选 + score prompt 体现，不用 embedding**。
> 代价：保留重复事件不去重；好处：流水线复杂度降一档，不依赖第三方 embedding API。

## 两条架构铁律

挂墙规则，跨所有 Step：

### 铁律 A：模型分工——预筛省钱、评分别省

| 阶段 | 模型                                                                              | 任务                                  | 为什么是这个                                                                     |
| ---- | --------------------------------------------------------------------------------- | ------------------------------------- | -------------------------------------------------------------------------------- |
| 预筛 | **kimi-cli**（本地 v1.40+，`thinking=False`）                                     | 二分类：是否 AI 相关                  | 本地零额外成本；二分类对模型智力要求低；CoT 只会拖慢                             |
| 评分 | **deepseek-v4-flash**（默认）/ deepseek-v4-pro / 可选 kimi-cli (`--backend kimi`) | 4 维打分 + 类别 + 中文摘要 + 推荐理由 | 判断"哪个公司在风口、这论文是否新颖"是**世界知识广度**问题，不能为了省钱用小模型 |

> 默认从 V4 Pro 改回 V4 Flash 是因为本项目 fan-out 大、批处理稳定，flash 的速度优势压过 pro 的边际质量增益；如果你的源更偏论文/纯学术，`.env` 里 `DEEPSEEK_MODEL=deepseek-v4-pro` 立切。
> 旧 `deepseek-chat` / `deepseek-reasoner` 2026-07-24 弃用。`--backend kimi` 时 `thinking=False`（CLI 显式传），CoT 不会改善 4 维打分。

卡兹克原话："V4 Pro，世界知识极强，在这种需要世界知识判断的任务下..."——评分阶段降级模型 = 整个系统废掉。

实现：`ai_radar/pipeline/_llm.py` 抽象出 `LLMBackend` Protocol，目前两个实现：`KimiCLIBackend`（subprocess）+ `DeepSeekBackend`（OpenAI-兼容 HTTP）。换 backend 不改主流程。

### 铁律 B：AI 处理 push 入库，展示层零模型调用

```
[抓取] -> [预筛] -> [评分] -> [加权 + 阈值精选] -> [DB 写入]
                                                       │
              ┌────────────────────────────────────────┤
              │                  │                     │
            Web 时间线         每日 Markdown        未来：邮件 / RSS / Telegram
              │                  │                     │
              └─────── 100% SELECT + 模板，零模型调用 ──┘
```

- 所有 LLM 调用集中在**入库流水线**：`is_ai_related / 4 维 scores / category / summary_zh / reason / total / is_selected` 一次写好。
- 任何展示形态（Web `web/app.py` / Markdown `pipeline/report.py` / 未来邮件 / RSS / Telegram bot）**100% 走 SELECT + Jinja 模板，不再调任何模型**。
- 卡兹克原文："日报本身不需要任何大模型来生成... 1 秒就能做出来。"
- 含义：DB schema 必须把 AI 处理的所有产物全部持久化；新加展示形态零成本。

---

## 五个核心机制

### ① 来源维护：sources.toml 是 single source of truth

- 用户编辑 `sources.toml`，程序"同步到 DB"；DB 是镜像，不是源。
- toml 增 → DB INSERT；toml 改 → DB UPDATE；toml 删 → DB **软删除**（`active=0`，保留历史 items 不孤立）。
- 程序**永远不写 toml**，只读。
- 调 tier、改 active、加新源都只动 toml，下次 fetch 即生效。
- 实现：`ai_radar/config.py::load_sources` + `ai_radar/db.py::sync_sources`

新增源的具体路径见 [`ADDING_SOURCES.md`](./ADDING_SOURCES.md)：原生 RSS / RSSHub / 公共 wechat2rss（xlab.app + bestblogs.dev 双实例）/ WeWe RSS / 微信读书 cookie 直连 / 起点小说 qidian fetcher / 自建 fetcher。

### ② 初筛：kimi-cli 批处理

```bash
kimi-cli -p "<prompt + JSON array of items>" --quiet --afk -y --no-thinking
```

**关键决策**：批处理。单条调用启动开销 1-3s，每天数百条会慢死。一次塞 40 条标题+摘要(intro+outro)，要求模型返回 JSON 数组：
```json
[{"id": 12, "ai": true}, {"id": 13, "ai": false}]
```
- prompt 在 `prompts/prefilter.md`
- 解析后 UPDATE `items.is_ai_related`
- JSON 解析失败的批次保持 NULL，下次再试
- **summary 截取**：`_intro_outro` 取前 1-2 段(≤300 字符) + 最后 1 段(≤150 字符)，中间用 `[…]` 占位。RSS 原文 600+ 字时只看 lede + 收尾，对二分类已够；批量大小从 20 升到 40 也不爆 token。
- 实现：`ai_radar/pipeline/prefilter.py`

### ③ 评分：DeepSeek 4 维 + 类别 + 摘要 + 推荐理由

模型只做判断，**不打总分、不判精选**。一次返回每条 5 个字段：

```json
{
  "id": 12,
  "scores": {"hardcore": 8, "primary_src": 9, "density": 8, "novelty": 6},
  "category": "技术研究",
  "summary_zh": "...",
  "reason": "..."
}
```

**4 维含义**（`prompts/score.md` 里有 0/3/7/10 分刻度对照）：

| 维度                     | 0 分          | 5 分             | 10 分                   |
| ------------------------ | ------------- | ---------------- | ----------------------- |
| **hardcore** 硬核度      | 鸡汤推文      | 高层产品介绍     | 详细算法/系统/数学推导  |
| **primary_src** 一手程度 | 转发+一句感想 | 资深博主深度解读 | 实验室原始论文/官网原文 |
| **density** 信息密度     | 纯营销/PR     | 平衡观点+事实    | 全数字+引用             |
| **novelty** 新颖度       | 重复报道      | 显著改进/新方法  | 首次提出/突破           |

> 历史注：weights.toml 里有第 5 维 `relevance_to_me`（embedding 余弦的占位），权重设为 0，目前不参与计算。Step 3 砍了。

**4 个类别**（`weights.toml [categories].all`）：
技术研究 / 产品发布 / 行业经济 / 技巧与观点。

> 历史注：2026-05-07 从 6 类(论文研究/infra工程/模型发布/产品发布/行业经济/技巧与观点)合并到 4 类。
> 论文/infra/模型发布在用户信源里事实上无法稳定区分(同一篇大厂技术解读三者皆是)；
> 区分技术深度的活儿全交给 hardcore 维度。`scores.category` 字段已迁移所有旧记录到 `技术研究`。

实现：`ai_radar/pipeline/score.py`，依赖 `_llm.py` 的 LLMBackend。

### ④ 加权 + 阈值精选（纯代码，无 LLM）

幂等可重跑。读 `weights.toml`，对每条已评分项算：

```
weighted_avg = Σ(dim_i × w_i) / Σ(w_i)         # 0-10 scale
total        = weighted_avg × tier_multiplier   # T1=1.20 / T1.5=1.00 / T2=0.85
is_selected  = (total ≥ thresholds[category][tier])
```

**二维阈值**（`weights.toml [selection.thresholds]`）模拟卡兹克"OpenAI 60 分过、KOL 60 分不过"——同一总分 T1 进精选、T2 不进。共 6 类 × 3 tier = 18 个数。

实现：`ai_radar/pipeline/weight.py`。命令：`uv run radar weight`，改完 `weights.toml` 后任意时刻可重算，**不烧 LLM 钱**。

### ⑤ 数据存储：SQLite 单文件

为什么 SQLite：单用户、单机、零运维、ACID 够用、`cp data/radar.db backup.db` 就是备份、FastAPI 直连无压力。

```
sources       sources.toml 的镜像，附加 etag/last_modified/last_fetched_at
items         每条原始信息一行；url UNIQUE 是 dedup 第 1 道；dedup_key 是第 2 道(知乎 N 人赞同同一篇 → 一行展示)
fetch_runs    每次抓取留一行：起止时间、新增条数、错误（运维可观测）
scores        每条已评分项一行：4 维 + category + summary_zh + reason + total + is_selected + model
feedback      Web UI 的 👍 👎 写这里；signal ∈ {thumbs_up, thumbs_down, hidden, saved}; UNIQUE(item_id, signal) → 同一信号点第二次取消
```

**跨源 dedup（`items.dedup_key`）**：知乎 RSSHub 把"X 赞同了回答: <Y>"做成 N 个 pin URL，原文同一篇。`db._compute_dedup_key` 用正则 `^.+?(?:赞同|...)了(?:回答|...)?[:：]\s*(.+)$` 抽 canonical title 作 key。`selected_items` / `all_scored_items` 用 `WITH ranked AS (...) SELECT * WHERE rn=1` window-function CTE 折叠到最高分代表行，并暴露 `endorsement_count`，前端 ≥2 时显示 `👥 N 人推荐`。

字段生命周期（按 Step 推进）：

| 时机              | 写入                                                                                  |
| ----------------- | ------------------------------------------------------------------------------------- |
| 入库（fetch）     | `items` 主行：source_id / url / title / summary / raw_content / author / published_at |
| 预筛（prefilter） | `items.is_ai_related`                                                                 |
| 评分（score）     | `scores` 子行：4 维 + category + summary_zh + reason + model                          |
| 加权（weight）    | `scores.total` + `scores.is_selected`                                                 |
| 反馈（Web POST）  | `feedback` 一行                                                                       |

> `items.embedding` / `items.event_id` 是 Step 3 占位列，目前永远 NULL。

### ⑥ 定时获取：cron + 文件锁

`crontab.example` 里全套：

```cron
0  */2 * * *  radar fetch         # 每 2h 抓 RSS / WeRead 等
30 */3 * * *  radar prefilter --limit 200
45 */4 * * *  radar score --limit 30
50 4   * * *  radar weight        # 每天 04:50 UTC 重算精选
5  0   * * *  radar report        # 每天 00:05 UTC 写日报 markdown
```

- **2 小时抓取频率**：T1 RSS 平均日更 1-3 条；arXiv 一天才更新一批；微信公众号 wechat2rss 24h 延迟。
- **防并发**：`fetch` 启动时 `fcntl.flock(data/.fetch.lock, LOCK_EX|LOCK_NB)`，已被占用直接退出。
- **可观测**：每次跑 INSERT `fetch_runs`。`uv run radar status` 一句话看健康度。
- Web 服务长跑，不在 cron 里，自己用 tmux/systemd 起：`uv run radar serve --host 0.0.0.0 --port 18086`

### ⑦ 不重复获取：三道关卡

| 层             | 机制                                                                                                    | 强度                                   |
| -------------- | ------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| 1. **DB 层**   | `items.url UNIQUE` + `INSERT OR IGNORE`                                                                 | ⭐ 强保障                               |
| 2. **HTTP 层** | 存 `sources.etag` / `last_modified`，下次请求带 `If-None-Match` / `If-Modified-Since`，304 直接跳过解析 | 中（省带宽）                           |
| 3. **应用层**  | URL normalisation：去 `utm_*` / `fbclid` 等追踪参数 + 去 fragment                                       | 小（防同条目带不同追踪 ID 被算成两条） |

**RSS fetcher 用 httpx 30 秒超时**（`fetchers/rss.py`）——不是 feedparser 默认（无超时，我们曾被 14 分钟卡死过）。

---

## 配置驱动：两个 toml + 一个 .env

项目根仅有的用户面向配置：

| 文件           | 调什么                          | 何时生效              |
| -------------- | ------------------------------- | --------------------- |
| `sources.toml` | 信源 增/删/改/active            | 下次 fetch            |
| `weights.toml` | 4 维权重 + tier 乘子 + 二维阈值 | 下次 `radar weight`   |
| `.env`         | API key / cookie                | 下次任意 `radar` 命令 |

权重和阈值**绝不硬编码进代码**。卡兹克原文："公式里调一下权重或者某个数值，几秒的事。"

---

## 解耦设计：现状文件树

```
ai_radar/
├── _env.py                  # .env 加载, 兼容 export 前缀, 大小写宽容
├── config.py                # 加载 toml -> dataclass (Source, Weights, Config)
├── db.py                    # 纯 SQL: schema + sync_sources + upsert_item + score helpers
├── fetch.py                 # 主入口 (radar fetch); fcntl 锁 + dispatcher
├── cli.py                   # typer 命令: fetch / status / sources / prefilter / score / weight / report / serve / weread-list
├── fetchers/
│   ├── base.py              # Protocol Fetcher + Item dataclass + FetchResult
│   ├── rss.py               # ✅ httpx + feedparser, 含 ETag/304 + URL normalize
│   ├── weread.py            # ✅ 微信读书 web API (cookie 鉴权, mp/shelf/book 三模式)
│   ├── qidian.py            # ✅ 起点小说聚合: 零网络 I/O, 纯读 data/qidian_progress.json; "领先 N 章才推" 语义
│   └── __init__.py          # FETCHERS dispatcher: rss / weread / qidian
├── pipeline/
│   ├── _llm.py              # LLMBackend Protocol + KimiCLIBackend + DeepSeekBackend
│   ├── _batch_llm.py        # ⭐ BatchedLLMStep ABC: prefilter + score 共享的批处理骨架
│   ├── prefilter.py         # ✅ kimi-cli 二分类 (BatchedLLMStep 子类)
│   ├── score.py             # ✅ N 维评分, 默认 deepseek-v4-pro (BatchedLLMStep 子类); dims 从 weights.toml 读
│   ├── weight.py            # ✅ 纯代码 加权 + 阈值精选; 一个 transaction 一把写完
│   └── report.py            # ✅ 每日 6 版块 markdown 生成
└── web/
    ├── app.py               # ✅ FastAPI: / /all (paginated) /daily/<date>(prev/next nav) /category/<n> POST /feedback/<id>
    ├── templates/           # base.html(健康 banner) / timeline.html(分页底栏 + 推荐数 badge) / daily.html(日历导航条)
    └── static/style.css     # 暗色卡片 UI, 类别 4 色
```

**Web UX 细节**：
- **/all 分页**：底部翻页（`?page=N&size=M`，size 边界 [10,200]），dedup_key 折叠后再分页计数
- **/daily 日历导航**：默认重定向到最近一个有内容的日期；页面顶部条带列出最近 14 天内**有精选条目**的日期，点击跳转；空日期不渲染
- **健康 banner**：`base.html` 一直挂；`db.fetch_health` 一查最近 fetch_run，发现 `weread-auth: errcode=-2012` / RSSHub HTTP 403/503/timeout / 其他 ≥3 个错就弹 warn/error 横幅，提醒该刷 cookie 或重启 RSSHub
- **推荐合并 badge**：`endorsement_count > 1` 时卡片头显示 `👥 N 人推荐`，提示这条被多源/多人重复触达过

**关键**：加新 fetcher 只动 `fetchers/`；加新 pipeline step 只动 `pipeline/`；加新 LLM backend 只动 `_llm.py`。主流程零改动。

---

## 实现状态（2026-05-07）

| Step    | 内容                                                                          | 状态                                          |
| ------- | ----------------------------------------------------------------------------- | --------------------------------------------- |
| **1**   | 信源 + RSS 抓取 + SQLite 入库 + 去重 + cron + 22 个英文 RSS                   | ✅                                             |
| **2.a** | kimi-cli 预筛                                                                 | ✅                                             |
| **2.b** | DeepSeek V4 4 维评分 + 摘要 + 推荐理由                                        | ✅                                             |
| ~~3~~   | ~~embedding + 聚类 + relevance_to_me~~                                        | ❌ **砍掉**（用户决策：taste through prompts） |
| **3.5** | 加权 + 二维阈值精选                                                           | ✅（必要的最小 Step 3 部分）                   |
| **4**   | FastAPI Web 时间线 + Markdown 日报 + 反馈 API                                 | ✅                                             |
| **5.a** | 公众号接入：wechat2rss 14 个 + WeRead 直连 5 个 + 量子位官方 RSS 1 个 = 20 个上桌 | ✅                                             |
| **5.b** | WeRead cookie 直连 fetcher（`/web/mp/articles` + `scripts/weread-keepalive.sh` v2 走 POST `/web/login/renewal` 真续期, 过期自停） | ✅                                             |
| 5.c     | X (RSSHub) 抓取；arXiv 摘要专用 fetcher                                       | ⏳                                             |
| 5.d     | Anthropic / Meta 之类无 RSS 站的 scrape fetcher                               | ⏳                                             |
| 6       | 微信公众号深度集成（WeWe RSS 自建）；趋势预测；热度指数                       | ⏳                                             |

---

## 不被列入但记下来的"AIHOT 智慧"

- **宁缺毋滥**：信源 168 个调了一个月。每加一个新源前问"真的需要吗"。
- **评分维度从隐性判断逆向**：在迭代评分 prompt 时，自己刷两天 arXiv，记录"为什么这篇我点开看了，那篇跳过了"，再把规律抽成 4 维。
- **能用脚本就别用 Agent**（卡兹克原话）：模型只打多维分，加权/阈值/聚类全代码。本项目实现就是这条铁律。
- **量化回测**（Step 3.5 之后可做）：写一个 backtest 脚本，给定一组 weights.toml 对历史 N 条重打分，对比"你心中的 ground truth"。
- **反馈点赞调权重**：Web 已加 👍 👎 → feedback 表自然累积，月度回顾哪些类别 👎 集中 → 调 weights.toml 阈值。

---

## 这个项目踩过的坑（2026-05 更新）

> 公开博客 / Stack Overflow 都给过的错误信息，记下来防止下次再撞。

### 1. WeRead cookie 续期：主页 GET 不行，只有 `/web/login/renewal` 才行

[Hank's Blog 2022-05](https://zhaohongxuan.github.io/2022/05/16/how-to-relong-cookies-in-weread/) 流传最广，
说"主页 GET 自动 Set-Cookie 续期"。**2026 实测无效**——服务端不在 `/` 上发新 `wr_skey`。
真正的端点是 `POST https://weread.qq.com/web/login/renewal` (Content-Type: application/json, body
`{"rq":"%2Fweb%2Fbook%2Fend","ql":false}`)。验证来源：[funnyzak/weread-bot](https://github.com/funnyzak/weread-bot)
（活跃维护，4500+ 行）的 `_refresh_cookie()`。详见 `scripts/weread-keepalive.sh` 注释。

### 2. mawk 不支持 `IGNORECASE` (HTTP/2 强制小写 header)

Linux/Ubuntu 的默认 `awk` 是 mawk，不是 gawk。`BEGIN{IGNORECASE=1}` 在 mawk 上**没效**。
HTTP/2 把所有 response header 小写化（`set-cookie:` 不是 `Set-Cookie:`），如果脚本依赖大小写敏感
匹配会全 miss。改用 `grep -i` + `sed -E ... /I` 解决。

### 3. kimi-cli `max_steps_per_turn` 默认 100，纯 LLM 任务必加 `--max-steps-per-turn 1`

`~/.kimi/config.toml::loop_control.max_steps_per_turn = 100`。我们做 JSON 提取根本不需要 agent
工具调用，但默认会跑 30-90s/batch 在文件扫描 / workspace 探查上。`--max-steps-per-turn 1` 强制
单步推理，速度回到接近纯 LLM API 调用。

### 4. Kimi for Coding HTTP API (`api.kimi.com/coding/v1`) 限制官方 agent 客户端

服务端用 `User-Agent: KimiCLI/<ver>` + `X-Msh-Platform: kimi_cli` + `X-Msh-Device-*` 一组 header
识别合法客户端。可以模拟（见 `_llm.py::KimiAPIBackend._build_headers()`），但本机环境里有 mihomo
反向隧道（127.0.0.1:65530），HTTP 流量会绕回笔记本再出网，**反而比 subprocess 慢 3×**（17s vs 5.6s
prefilter benchmark）。结论：**默认走 subprocess**，HTTP 留作 opt-in (`--backend kimi-api`)。

### 5. Score 输入截断必须用 intro+outro，不能 naive truncate

`score.py` 早期用 `_truncate(s, 800)` 头部硬切。对长 Zhihu 回答（17KB+）/公众号长文，前 800 字
往往是寒暄 + 摘要重复，干货在中段或结尾。改用 `_intro_outro(s, 800, 400)`（同 prefilter 实现）：
头 800 + `[…]` 占位 + 尾 400。BBuf 那条 17KB DPSK 实测从"完全离题"变成正确分类。

### 6. 用户的 `sk-kimi-*` token 是 OAuth bearer 不是 API key

kimi-cli `login` 后存在 `~/.kimi/credentials/kimi-code.json`。可作 Bearer 直接 HTTP，但只对
`api.kimi.com/coding/v1` 有效，需配合上述 `X-Msh-*` headers。Moonshot 公开平台 API
(`api.moonshot.cn/v1`) 用的是 `sk-...` 前缀的不同 key。两套体系不互通。
