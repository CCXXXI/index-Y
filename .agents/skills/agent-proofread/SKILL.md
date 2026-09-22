---
name: agent-proofread
description: "Use when agent 通读校对：按章分派子 agent 通读整卷 Y 产物，发现、复核并落规则。"
---

# agent 通读校对

proofreading 的前移：发现环节从「维护者阅读时记下」换成「子 agent 按块通读」，判定口径与规则落地不变。**开工选卷先查 `status.md`（各卷 AI 校对状态），优先上游与本项目都未校对的卷**；上游已全卷校对的只有创约 1/2/3（以 index-X git log「重新校对」提交为准，见 status.md 刷新口径），其余卷均未通校。

## 判定依据（指针，不复制进本仓库）

- 规范正文：`../index-X/docs/translation-spec.md`——尤其「四、语义与信息」（哪些必判缺陷）与「六、证据边界」（防误报）。
- 已裁定索引：`../index-X/.agents/skills/translation-term-unification/SKILL.md` §六 + `../index-X/docs/maintenance-records/`——**开工前必读**，已裁定锚点不得翻案。

## 流程

### 1. 切块分派子 agent

- `uv run python scripts/agent_proofread/plan_chunks.py <卷>`：把整卷正文切成 ≤10 块（delegate_task 并行上限），每块 ~1.5 万字符（大卷自动加大），主区间无缝拼接、前后 ~10 行重叠上下文。包装页（Cover/Illustrations/Information 等）自动跳过。
- 提示词模板 `templates/chapter-agent-prompt.md`：替换 `{{VOL_PREFIX}}` 后作 `delegate_task` 的 context，goal 用 plan_chunks 输出的逐块 goal 行；模板尾部附 output_schema。一次调用并行分派全部块。
- 子 agent 回原文核对用 `scripts/proofreading_locate.py`，模板要求**批量一次跑完**（试跑教训：一词一查会让单 agent 拖到 40+ 轮）。
- 时效目标：**单块 <15 分钟**——Kimi 上下文缓存约 20 分钟过期，超长会话失去缓存命中（S1_01 按章分派时 3.8 万字大章单 agent 跑了 30 分钟）。

### 2. 三层复核（子 agent 产出是自报，不复核不落规则）

1. **机械验证**：把各子 agent 返回的 findings 数组合并为一个 JSON 文件（保留 file/line/quote/jp_evidence 等字段），跑 `uv run python scripts/agent_proofread/verify_findings.py <卷> findings.json`——中文引文逐字验证（剥标签容差）、日文依据语料验证、X/Y 比对。块间重叠上下文行若被多个子 agent 重复上报，按 (file, line) 去重。
2. **X≠Y 归因**：`x_same=false` 的条目查 rules/——良性归一化（如 塑胶→塑料）按上游问题处理；**规则改坏的是规则回归**（案例：`_common` 安心感→安全感 把 X 的正确译法改错），落分卷规则压回，不回改通用规则。
3. **人工判定**：误译高置信条目逐条回原文终判；警惕**模式性误报**——单章看似衍字、全卷实是统一体例（案例：四章标题均带人称「她/他」，单报一章即误报）。

### 3. 落规则

- 候选规则写 TSV（`旧<TAB>新<TAB>注释` 三列，注释含原文依据；旧串为 X 侧 xhtml 逐字，ruby 标签内联）。
- `uv run python scripts/agent_proofread/apply_rules.py <卷> candidates.tsv [--replace 旧串]...`：归属计数（全语料命中须全在本卷，否则报错交人裁决）、正则守卫、双模拟，全过才写 TSV。
- 重跑 `uv run python scripts/sync/x2y.py`，`--verify-y` 终验；`git diff` 应为纯行内替换（+N/−N 相等）。
- rules/ 与 Y/ 同一 commit：`fix: 校正 <卷标识>（N 处文本修订）`，body 附全量 `旧→新`。
- **登记 `status.md`**：更新该卷「本项目」列为日期（提交号，修订处数），随本批改动一起提交。

## 经验

- 子 agent 引文跨 ruby 标签或跨行：raw 不匹配≠编造，verify_findings 已含容差；仍不符才算编造。
- 原文仓库部分文件的读音写在 `<rt>` 之外（如 安堵あんど）：jp_ok=False 偶属此类误报，遇此取 locator 的原文命中上下文人工核一眼，勿直接判编造。
- 既有规则可能与新规则抢同一文本：长规则取代时用 `--replace` 删旧短规则（案例：估计大概→估计）；分卷先于通用应用，顺序即真理。
- TSV 表头必须保留——丢表头 x2y 校验直接拒收（本流程犯过）。
- 子 agent 自报的「读了多少字/查了多少词」不可信，以脚本验证为准。
- 成本参考（S1_01，按章分派旧方案）：11 万字符卷 56 发现、误报 <15%、6 agent 31 分钟；子 agent 原文回查接近遇疑必查（全卷 164 词），订阅额度按百万 token/卷估。切块 + 批量查询后墙钟应压到 ~15 分钟级。
