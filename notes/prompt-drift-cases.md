# Prompt drift cases — 攒着, 下一轮 prompt 迭代时统一处理

> 本文件**不是**永久文档. 用作积累具体的 prompt 失败案例 — 满足够多了再回头改 prompt.
> 处理完后整理成 prompts/score.md / prompts/prefilter.md 的反模式后, 删除本文件.

---

## Case 1 (2026-05-07): BBuf 赞同了 DPSK V4 review — score 离题万里

**输入到 score 的 item**:

- title: `BBuf赞同了回答: DeepSeek V4 预览版本上线并同步开源，哪些亮点值得关注？`
- summary: 17KB 实测 review (2.5亿 Token / TileLang / FlagOS / Claude Code Agent 实战)
- score 实际看到的 summary: 仅前 800 字符 (score.py 当时 `_truncate(s, 800)`)

**模型输出**: 把它按"DPSK V4 模型发布讨论"打分 + 写理由,
丢失了真正的 hook ("2.5亿 Token 实测 + 编译器视角"); reason 写成 BBuf 自己解读.

**根因**:
1. ⭐ Zhihu title 是**问题**不是答案 — model title bias 把分类带跑
2. ⭐ score 当时用 naive truncate 头 800 字符 — 已修复改 _intro_outro 800+400
3. ⭐⭐ 没把 "endorser=BBuf" 作为独立字段送出去, 仅靠 prompt 反模式 #6
   靠模型自己解析 title 字符串 — 脆弱
4. 真实回答者的名字 (被 BBuf 赞同的那位) 我们根本没存 (RSSHub feed 里 author 字段被 BBuf 覆盖)

**下一轮 prompt 改的方向**:
- score 输入 schema 加 `endorser` / `original_author` 字段
- prompt 加 Zhihu 专门 section: title 是问题; summary 是答案; 评分基于 summary 不基于 title
- 也许 fetchers/rss.py 加 zhihu 后处理: 从 description 抽答主名字

---

## Case 2 (2026-05-07): SiriusNEO 高中生选专业 — prefilter false positive

**输入到 prefilter 的 item**:

- source: `知乎 / SiriusNEO`
- title: `SiriusNEO赞同了回答: 上海交大ACM班和清华大学强基计划信计专业该怎么选？`

**prefilter 输出**: `is_ai_related = 1` (放过去到 score 了)

**实际内容**: 高中生 CS 大学选择专业讨论, 跟 AI 行业本身关系不大.
被赞同 ≠ AI 大佬转的就一定是 AI 内容.

**根因**:
1. ⭐⭐ prefilter prompt 写 "不确定就 true" — 偏置在累积噪音
2. ⭐ "信计 / ACM班" 这类 CS-adjacent 关键词容易让模型当作 AI 相关
3. 知乎 source 默认全是 AI 大佬, 实际 ta 们也会赞同非 AI 内容

**下一轮 prompt 改的方向**:
- prefilter 收紧 "不确定就 true" — 改成 "明确非 AI 主题 (高考/择校/职场八卦/政经无 AI 角度) 一律 false"
- 单独加 Zhihu false-positive 反模式: 大佬赞同 ≠ AI 相关
- 也许 add 几条具体反例进 prefilter prompt

---

## Case 3 (2026-05-07): SiriusNEO 招生广告 — 又一例 prefilter false positive

**输入**: title `SiriusNEO赞同了文章: 2025年清华交叉院人工智能方向直博生招生广告_贺天行`

**问题**: 是 PhD 招生广告, 不是 AI 内容. 但 title 里有 "人工智能方向" 关键词, 触发 prefilter 放行.

**根因**: 同 Case 2 — 关键词命中 ≠ AI 主题相关. 招生 / 招聘 / 课程介绍这类**机构活动通知**应该一律 false, 即便提到 AI/ML.

**下一轮 prompt 改的方向**:
- prefilter prompt 反模式加: "招生/招聘/活动通知/讲座预告**即便涉及 AI** 也归 false — 这些是机构动作, 不是 AI 内容本身"

---

## Lookback 模板 (将来加新 case 时)

每加一条 case, 至少写:
- 哪个步骤出错 (prefilter / score / weight)
- 输入 (title / source / summary 长度)
- 模型实际输出 vs 期望
- 根因 (3-5 条, 标 ⭐ 重要度)
- 下一轮该改 prompt / schema / fetcher 哪一处
