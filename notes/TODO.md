# Project TODOs

> 临时积压点，做完就划掉/删除。这是开发节奏自留笔记，不当 docs 用.

## 🟡 多消费者锁：score / prefilter 并发去重

**触发场景**：用户同时在两个 tmux 跑 `radar score --backend kimi`，或者 cron 跑 score 的同时手动也跑了一次。两个消费者会读到同一批 `pending_for_scoring()`、各自调一遍 LLM、`upsert_score` 后写入同一行（PK on item_id, ON CONFLICT UPDATE → 第二次写覆盖第一次）。**数据没坏，但 LLM 配额浪费一倍**。

**生产者/消费者建模**：
- 生产者: `radar fetch` (单例, 已有 fcntl flock)
- 消费者: `prefilter` 消费 `is_ai_related IS NULL`、`score` 消费 `is_ai_related=1 AND NOT IN scores`
- 多消费者之间**任务集合不应重叠**

**实现方向（任选其一）**：

1. **简单 flock**（5 分钟见效）
   - `radar score` / `radar prefilter` 启动各自抢 `data/.score.lock` / `.prefilter.lock`
   - 没抢到就退出（"another <step> is running, skipping"）
   - 缺点：禁止任何并发，连不同 LIMIT 也不能并行

2. **claim columns**（半小时见效, 推荐）
   - items 加 `claim_owner TEXT` + `claim_at TIMESTAMP`
   - 启动时给自己生成一个 owner UUID
   - 每批用一条原子 UPDATE 抢 N 条:
     ```sql
     UPDATE items SET claim_owner=?, claim_at=CURRENT_TIMESTAMP
     WHERE id IN (
       SELECT id FROM items
       WHERE is_ai_related IS NULL AND claim_owner IS NULL
       LIMIT ?
     ) RETURNING id
     ```
   - 处理完写 score/is_ai_related 后清 claim
   - 启动时回收 stale claim (`claim_at < now - 1 hour`)
   - 优点：天然支持多消费者并发，每个消费者拿不同行
   - 兼容 SQLite（`UPDATE ... RETURNING` 在 3.35+ 支持）

**优先级**：低。当前一个人跑，碰不到。等真的开始多 cron 或 distributed 再做。

**Markers in code**：
- `ai_radar/db.py::pending_for_scoring`
- `ai_radar/db.py` prefilter pending 查询
- `ai_radar/pipeline/_batch_llm.py::run`

---

## 🟡 prefilter prompt 收紧：AI-adjacent ≠ AI-relevant

**触发场景**：当前 `prompts/prefilter.md` 写 "不确定就 true (让下游评分阶段进一步过滤)"——
为减少漏判设计的，结果偏置太松。**有些条目能扯上 AI 但用户不感兴趣**，照样穿过去消耗 score 配额。

已积累 bad case (见 `notes/prompt-drift-cases.md` Case 2 / Case 3):
- 高考/择校讨论 (CS / 信计 / ACM 专业, AI-adjacent 但非 AI 内容本身)
- 机构活动通知 (招生广告 / 招聘 / 讲座预告, 即便 title 含 "人工智能方向")
- 大佬转发的非 AI 内容 (知乎大V 赞同了与 AI 无关的回答)

**收紧方向**：

1. 反模式专列一节 (类似 score prompt 的 ⚠️ 块):
   - "讲择校/职业规划/招生招聘/培训课程**即便提到 AI/ML 关键词**也归 false"
   - "知乎大佬赞同 ≠ AI 内容"
   - "硬件/CPU/编译器**与 AI 无明确关联**的归 false (e.g. 通用 OS 调度优化)"
2. "不确定就 true" 改成 "**明确非 AI 主题**直接 false; 真模糊才 true"

**优先级**：中。每个穿透的 false-positive 都白烧 score 一次。攒够 ~10 case 一起改 prompt
比改 N 次稳。

**Markers in code**：
- `prompts/prefilter.md` (the prompt file itself)
- `notes/prompt-drift-cases.md` (case 库, 加新 case 写这里)
