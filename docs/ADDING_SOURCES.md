# 新增信源 Cookbook

> **核心洞察**：大多数"非 RSS"平台（公众号、知乎、小红书、X、B 站）都可以通过 [RSSHub](https://docs.rsshub.app) 这类**网关**转成 RSS。所以你 **99% 的情况只需要写一行 `[[source]]`，`fetcher = "rss"`**——根本不用改代码。

## 决策树

```
你想加的源 ──┬── 平台原生开放 RSS / Atom?
            │     └── 是 ──→ 直接 fetcher = "rss"，URL 填 feed (Substack / 大多数实验室博客 / Reddit / arXiv)
            │
            ├── 平台没 RSS, 但有 RSSHub / WeRSS 网关?
            │     └── 是 ──→ fetcher = "rss"，URL 填网关 URL (X / 知乎 / B 站 / 小红书 / 公众号)
            │
            └── 都没有, 必须爬 HTML?
                  └── fetcher = "scrape"  (Step 5 才实现, 现在写 active=false 占位)
```

## 各平台具体接入方式

### 1. 原生 RSS（最简单）

直接写 `[[source]]`，`fetcher = "rss"`，URL 填官方 feed：

```toml
[[source]]
name     = "Substack 某博主"
tier     = "T2"
category = "kol"
url      = "https://example.substack.com/feed"
fetcher  = "rss"
active   = true
```

常见原生 RSS 平台：
- 大多数官方博客（OpenAI / DeepMind / NVIDIA / PyTorch ...）
- Substack（`<sub>.substack.com/feed`）
- Reddit（`https://www.reddit.com/r/<sub>/.rss`）
- arXiv（`https://export.arxiv.org/rss/<category>`）
- GitHub releases（`https://github.com/<owner>/<repo>/releases.atom`）
- Hacker News（`https://hnrss.org/...`）

### 2. RSSHub 网关（X / 知乎 / B 站 / 小红书）

> ⚠️ **不要用 RSSHub 抓微信公众号** —— `/wechat/*` 路由依赖飘忽的第三方 mirror。本项目走 wechat2rss + WeRead cookie（路径 D）即可。
> ⚠️ **小红书半能用** —— 反爬严重，公共实例 403/429 多发，必须自建 + 换 IP 池才相对稳。不建议作为主信源。

[RSSHub](https://docs.rsshub.app) 是开源项目，把上百个平台转成 RSS。两种用法：

| 用法 | 优点 | 缺点 |
|---|---|---|
| **公共实例** `https://rsshub.app/...` | 零部署 | 限流严重、容易被风控、不稳定，2026 起公共实例已挂 Cloudflare 验证（普通 RSS 客户端基本拿不到） |
| **自建实例**（Docker 一行起） | 稳定、可控、有缓存、能注 Cookie 拿登录态内容 | 需要服务器 |

> **本项目走自建** —— 见 `infra/rsshub.docker-compose.yml`，绑回环 `127.0.0.1:41200`（端口故意偏远 + 内网，不暴露公网）。Cookie 等敏感配置写 `infra/rsshub.env`（gitignore）。
>
> ```bash
> docker compose -f infra/rsshub.docker-compose.yml up -d
> # 知乎登录态填 ZHIHU_COOKIES (zhihu.com → DevTools Cookies → 复制 z_c0/d_c0/_xsrf 等)
> ```
>
> sources.toml 里所有 `rsshub.app` URL 都改成 `http://127.0.0.1:41200`；批量替换可跑 `bash scripts/rsshub-rewrite.sh` (支持 `--activate`)。

URL 模板（把 `rsshub.app` 换成你的实例域名）：

```toml
# X / Twitter — 用户时间线
url = "https://rsshub.app/twitter/user/<handle>"
# 例：@karpathy → https://rsshub.app/twitter/user/karpathy

# 知乎 — 用户回答
url = "https://rsshub.app/zhihu/people/answers/<urlToken>"
# 知乎 — 用户文章 (专栏 / 想法)
url = "https://rsshub.app/zhihu/posts/people/<urlToken>"
# urlToken 怎么找：知乎个人主页 URL 末段，如 zhihu.com/people/li-mu-9 → "li-mu-9"

# 知乎专栏
url = "https://rsshub.app/zhihu/zhuanlan/<id>"

# B 站 UP 动态
url = "https://rsshub.app/bilibili/user/dynamic/<uid>"
# uid 怎么找：UP 主页 URL 末段，如 space.bilibili.com/12345 → 12345

# B 站 UP 视频投稿
url = "https://rsshub.app/bilibili/user/video/<uid>"

# 小红书用户笔记
url = "https://rsshub.app/xiaohongshu/user/<userid>"
# userid 是小红书 profile URL 末段；XHS 反爬严重，自建 RSSHub 也常炸

# 微信公众号 — 三种路径，都不省心：
# (a) 通过 RSSHub 的 wechat 路由（依赖第三方镜像，飘忽）
url = "https://rsshub.app/wechat/...."
# (b) WeWe RSS（自建，需要绑微信 app 账号；最稳）
url = "http://localhost:4000/feeds/<feedId>"
# (c) WeRSS（付费托管）
url = "https://werss.app/feed/<id>.atom"
```

具体路由参数请查 [RSSHub 文档](https://docs.rsshub.app/routes/)。

### 3. 微信公众号——为什么是黑洞

微信故意不开放 feed，且通过 ip + ua + cookie 三重风控。能用的方案都有代价：
- **WeWe RSS**：自建 + 需要长期登录的微信小号，可能被封号
- **WeRSS / FeedHub**：付费，省心
- **抓包私人 RSSHub 镜像**：技术门槛高，飘
- **手动抓 mp.weixin.qq.com 文章 URL** 一篇一篇加：最笨但最稳，适合公众号更新慢

`ai-radar` 不在 Step 1 处理这个。Step 6 攻坚时再决定。

### 4. 公众号接入完整流程（专题）

公众号是最难啃的——微信刻意不开放 RSS。三条路径，按推荐度排：

#### 路径 A：两个**公共 wechat2rss 实例**互补查询（**推荐起步**）

中文圈有**两个**独立维护的 wechat2rss 公开实例。**先查一个，没命中再查另一个**——它们覆盖的公众号有交集也有差集：

| 实例 | URL 模板 | 列表页 | 收录倾向 |
|---|---|---|---|
| **xlab.app** | `https://wechat2rss.xlab.app/feed/<hash>.xml` | [list](https://wechat2rss.xlab.app/list/list) | 综合，~300+ |
| **bestblogs.dev** | `https://wechat2rss.bestblogs.dev/feed/<hash>.xml` | [BestBlogs OPML](https://github.com/ginobefun/BestBlogs/blob/main/BestBlogs_RSS_ALL.opml) | 偏 AI / 编程，~115 公众号 |

- ✅ 都免费、无需注册、24 小时更新延迟
- ⚠️ 两边都没有的话：再考虑路径 B/C

**操作步骤：**

1. **先 BestBlogs 的 OPML 一把抓**：
   ```bash
   curl -sL https://raw.githubusercontent.com/ginobefun/BestBlogs/main/BestBlogs_RSS_ALL.opml \
     | grep -i "公众号名"
   ```
2. 没命中 → 翻 [xlab.app 完整列表](https://wechat2rss.xlab.app/list/list)
3. 还没命中 → 去 [Wechat2RSS GitHub Issues](https://github.com/ttttmr/Wechat2RSS/issues) 提 issue 申请收录（**注意：一次提多个用一个 issue**，别开 N 个）
4. 命中后填进 `sources.toml` 对应条目，`active = true`
5. `uv run radar fetch` 验证

#### 路径 B：自建 WeWe RSS（要可控/低延迟时）

- ✅ 任何公众号都能订阅，0 延迟
- ⚠️ 需要绑定一个 WeChat 小号（扫码登录），可能被风控
- ⚠️ 需要长期维护（小号可能掉登录）

**Docker 一行起：**

```bash
docker run -d --name wewe-rss \
  -p 4000:4000 \
  -v $PWD/wewe-data:/app/data \
  cooderl/wewe-rss
```

- 浏览器开 `http://localhost:4000`，扫码登录小号
- 在面板搜公众号 → 添加订阅
- 拿 feedId，URL 形如 `http://localhost:4000/feeds/<id>.atom`
- 填进 `sources.toml`，`active = true`

#### 路径 D：微信读书 cookie（直连，**已能拉文章**）

✅ **2026-05-07 capability upgrade**：之前以为 WeRead web cookie 拉不到 公众号 正文（基于 `chapterInfos` 测试为空）；实际上 SPA reader 走另一套 endpoint `GET /web/mp/articles?bookId=<MP_WXS_*>`，**能返回每篇文章的 title / 摘要预览 (~120 字) / 发布时间 / mp.weixin.qq.com originalId**。够 ai-radar prefilter + score 用。

依然拿不到的：
- 完整文章正文（reader 页二次请求才取，体积大，本项目用不上）
- mobile-only `i.weread.qq.com` 的接口（不同 accessToken 鉴权）

**关键洞察**：在微信读书里，公众号被存成一种虚拟"书"——
- `bookId="MP_WXS_..."`：你订阅的每个公众号是一本"书"，type=3 标记
- `bookId="mpbook"` (title="文章收藏")：你随手收藏的散文章一锅烩
- 走 `/web/mp/articles?bookId=...` → 该 公众号 最近的 14-25 篇

**操作步骤**：

1. **拿 cookie**（含 HTTP-only `wr_vid` + `wr_skey`）：浏览器登录 https://weread.qq.com，**DevTools → Network tab → 任意请求 → Request Headers → Cookie:** 那行整串复制。
   > ❌ 别用 `copy(document.cookie)`——它读不到 HTTP-only，会缺关键 token。
2. **塞 .env**：
   ```bash
   weread_cookie="wr_vid=...; wr_skey=...; ..."
   ```
3. **发现已订阅的公众号**：
   ```bash
   uv run radar weread-list
   ```
   会打印 shelf 全部 books（公众号 是 type=3, bookId 形如 `MP_WXS_*`）。原始 shelf JSON 存到 `data/weread_shelf.json` 可 grep。
4. **激活某个 公众号** — 在 sources.toml 填：
   ```toml
   [[source]]
   name     = "公众号 / AI Infra之道"
   tier     = "T1.5"
   category = "kol"
   url      = "weread://book/MP_WXS_3010620007"
   fetcher  = "weread"
   active   = true
   ```
5. **或者一锅烩** — `url = "weread://shelf"` 会自动遍历 shelf 上所有 type=3 MP_* book，每个抓一遍。一行抓全部。
6. 跑 `uv run radar fetch` 验证。

**Item URL 形式**：`https://mp.weixin.qq.com/s/<originalId>` —— 公众号 文章的官方短链。

> **验证 2026-05-07**：之前以为 mp.weixin URL 一定捅到 captcha。实测带正常浏览器 header（`User-Agent: Chrome…` + `Referer: https://weread.qq.com/`）就直接 200 出正文。Web UI 里点击的是用户浏览器自身请求，浏览器会自动带这些 header，**直接可读**。
>
> 所以**不需要走 Sogou 微信搜索**做 title→URL 反查。试过：搜索页 (`weixin.sogou.com/weixin?type=2&query=...`) 能进，但点结果触发 anti-spider (`/antispider/?from=...`)，要 CAPTCHA + 维持 session，cron 不友好。直接用 `mp.weixin.qq.com/s/<originalId>` 又简单又稳。

**Cookie 时效性**：WeRead web session 不是月级长效——实测**几小时**就 `errcode=-2012 登录超时`。挂 cron 不太行，得盯 fetch_runs 错误日志、看到 `weread-auth:` 就重抓 cookie。如果长期使用建议自建 WeWe RSS（路径 B）反而省事。

**wechat2rss vs weread cookie 怎么选**：

| 维度 | wechat2rss (公共) | weread cookie (本机) |
|---|---|---|
| 覆盖度 | 仅 wechat2rss 收录的公众号 | 你 WeRead shelf 上的所有 type=3 公众号 |
| 延迟 | ~24h | 实时 (跟 WeRead app 同步) |
| 维护成本 | 0 | cookie 偶尔过期需重抓 |
| 适合 | 大众化 公众号 | 长尾 公众号 (PaperAgent / TsinghuaNLP / 工程芯一 等 wechat2rss 未收录的) |

**异常分类**（fetch 出错时 fetch_runs.errors 会带前缀）：

| 前缀 | 原因 | 怎么办 |
|---|---|---|
| `weread-auth:` | cookie 过期 / 401 / `errcode -2012` | 重抓 cookie |
| `weread-endpoint:` | 404 / 路径错 / `errcode -2003` | 代码 bug，等修 |
| `weread-response:` | 200 但 body 异常 | 临时问题，下次重试 |
| `weread-http:` | 网络层 timeout / connection reset | 网络问题，下次重试 |

#### 路径 C：WeRSS / 其他付费托管

- [WeRSS](https://werss.app)：月费托管版
- 直接给 RSS URL，省心，但要钱

#### 我们项目里公众号的现状（2026-05-07）

✅ **已激活 20 个 = wechat2rss 14 + WeRead 5 + 官方 RSS 1**：

走 **wechat2rss.xlab.app**（5）：字节跳动技术团队 / 阿里云开发者 / PaperWeekly / 腾讯技术工程 / 42章经

走 **wechat2rss.bestblogs.dev**（9）：机器之心 / 歸藏的AI工具箱 / 腾讯云开发者 / 赛博禅心 / AI寒武纪 / 真格基金 / InfoQ 中文 / 极客公园 / 数字生命卡兹克

走 **WeRead cookie 直连**（5，wechat2rss 未收录但用户 WeRead 已订阅）：AI Infra之道 / NeuralTalk / PaperAgent / 工程芯一 / 青稞AI

走 **量子位官方 RSS**（1）

⏳ **仍是占位 11 个**（既不在 wechat2rss 也不在用户 WeRead shelf）：TsinghuaNLP / 博古睿研究院 / NICE学术 / 知潜KnowFuture / InfraTech / 海外独角兽 / 范阳 Brainwave / 半导体行业观察 / 沧浪1 / 泥巴青年 / 爱喝咖啡的猪

→ 这 11 个想要的话: (a) 在 WeRead app 里订阅 → 重抓 cookie → 再跑 `radar weread-list` 找 bookId → 切到 weread:// fetcher; (b) 自建 WeWe RSS（路径 B）—— 一劳永逸方案。

**激活其中一个的 walk-through**（以 PaperAgent 为例）：

```bash
# 1. 提 issue 申请收录
open https://github.com/ttttmr/Wechat2RSS/issues
# 标题: 申请收录公众号 PaperAgent
# 正文: 公众号名 + biz id 或一篇文章 URL

# 2. 等几天 wechat2rss 通知收录, 拿到 URL 类似:
#    https://wechat2rss.xlab.app/feed/abc123...xml

# 3. 编辑 sources.toml, 把 PaperAgent 那条改:
#    url = "<上面的 URL>"
#    active = true

# 4. 同步到 DB + 抓取
uv run radar fetch

# 5. 流转下游
uv run radar prefilter --limit 50
uv run radar score --limit 30
uv run radar weight

# 6. 浏览器看
```

#### 公众号源的"特别注意"

1. **延迟**：wechat2rss 是 24h，别期望像 X 一样实时
2. **summary 经常很长**：评分 prompt 已经截到 800 字，但内容信息密度可能很低（PR 稿）
3. **HTML 残留多**：富格式可能漏，建议每次新加公众号后跟踪几条看 quality
4. **量大可能压满 prefilter 预算**：机器之心、PaperWeekly 这种大号一天可能 5-10 条；同时激活 6 个 → 30-60 条/天，把 kimi-cli `--limit` 调高
5. **重复内容**：多个 KOL 公众号可能转同一篇官方稿。事件聚类砍了，所以会重复显示——这是已知接受的代价（你说不要 Step 3 的事件聚类）

### 5. 必须自己写 fetcher 的情况

只有当一个站点：
- 没有官方 RSS
- 没有 RSSHub 路由
- 你又必须接

才需要自己写。流程：

1. 在 `ai_radar/fetchers/` 加新文件 `myplatform.py`，实现 `Fetcher` 协议（见 `fetchers/base.py`）：
   ```python
   from ai_radar.fetchers.base import FetchResult, Item, Fetcher

   class MyPlatformFetcher:
       name = "myplatform"

       def fetch(self, source) -> FetchResult:
           # 1. httpx.get(source.url, ...)
           # 2. 解析 HTML / JSON
           # 3. 返回 FetchResult(items=[Item(...), ...])
           ...
   ```

2. 在 `ai_radar/fetchers/__init__.py` 注册：
   ```python
   FETCHERS["myplatform"] = MyPlatformFetcher()
   ```

3. sources.toml 里 `fetcher = "myplatform"`，搞定。主流程零改动。

## 如何应用配置变更

```bash
# 1. 编辑 sources.toml
$EDITOR sources.toml

# 2. 下次跑 fetch, 程序自动 sync 到 DB
uv run radar fetch
# 输出会显示: "sources sync: +1 new, ~0 updated, -0 deactivated"

# 3. 单源临时关闭：把 active 改 false, 历史 items 不丢
# 4. 永久删除：从 toml 删掉, DB 里那个源会被软删除 (active=0), items 仍可查
```

## 加源的纪律

来自卡兹克 AIHOT 的经验（他 168 个源调了一个月）：

- **宁缺毋滥**：每加一个新源前问"我真的会读它的输出吗"。168 个源每天进 ~600 条，加错一个就是 +30 条噪音。
- **一手优先**：能找到官网 feed 就别用聚合站。
- **分级要狠**：T1 是经过同行评审 / 官方第一手发布（OpenAI / DeepMind / NVIDIA 官方博客等），T1.5 是同来源的官方 X 账号噪音版 + 信任的硬核公众号（NeuralTalk / 量子位等），T2 是个人/媒体/预印本。**arXiv 是 T2**：未经同行评审、标题党 + re-submit 多，要靠 hardcore/density 高分自己挣进精选。混淆等级 = 后续打分体系崩。
- **加完跑一周再说话**：观察这个源每天进多少条、点开率如何，再决定是否保留 / 调 tier。
