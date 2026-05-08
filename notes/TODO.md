# Project TODOs

> 临时积压点，做完就划掉/删除。这是开发节奏自留笔记，不当 docs 用.

## 🔴 arxiv 信息源处理：当前 inactive, 等专用 fetcher

**触发场景**：2026-05-07 用户决策：arxiv 三个 cs.LG / cs.DC / cs.AR RSS 噪声 vs 价值不划算，
关掉 active 标志暂停 fetch；同时清掉 DB 里 248 条不符合 (AI/Agent/RL infra) AND (知名公司 / 顶尖学校) 的旧 item。

**Why**：
- arxiv RSS 量大 (cs.LG 单日 100+)，**绝大多数与 AI infra 无关** (应用 ML/医疗/气候/纯数学)
- 现有 prefilter + score 只看主题不看机构，烧 LLM 配额收效甚微
- 当前 17 条幸存 = 真正想看的 (infra + 顶级出品), 是想要的目标分布

**重启 arxiv 的前提条件**：写一个专用 arxiv fetcher (不走通用 RSS)
- 多一道**机构白名单过滤** (只放行 affiliation ∈ 知名公司/顶尖学校)
  - 抓 abstract page 的 author affiliation 列表 (arxiv API 给得到)
  - 白名单见 `scripts/cleanup_arxiv.py` 的 PROMPT
- 多一道**主题硬约束** (cs.DC/cs.AR + 标题关键词白名单 [infra/training/inference/serving/kernel/compiler/scheduler/...])
- prefilter / score 阶段无需改

**markers**：
- `sources.toml`: 三个 arXiv 源 active=false
- `scripts/cleanup_arxiv.py`: 一次性清理脚本 (含机构 + 主题白名单, 可作未来 fetcher 的 spec)
- DESIGN.md: Step 5.c 提到 "arXiv 摘要专用 fetcher"

**优先级**：中. 暂停期间不烧成本 = 不急. 重启时一并写 fetcher.

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
