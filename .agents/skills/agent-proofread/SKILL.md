---
name: agent-proofread
description: "Use when agent 通读校对：按章分派子 agent 通读整卷 Y 产物，发现、复核并落规则。"
---

# agent 通读校对

proofreading 的前移：发现环节从「维护者阅读时记下」换成「子 agent 按章通读」，判定口径与规则落地不变。适用卷为**上游未做过 AI 校对的卷**（旧约、新约等；创约 1/2/3/6 上游已做过，Y 已继承成果，边际价值低——上游校对覆盖范围以 `../index-X` git log 的「重新校对/复核」提交为准）。

## 判定依据（指针，不复制进本仓库）

- 规范正文：`../index-X/docs/translation-spec.md`——尤其「四、语义与信息」（哪些必判缺陷）与「六、证据边界」（防误报）。
- 已裁定索引：`../index-X/.agents/skills/translation-term-unification/SKILL.md` §六 + `../index-X/docs/maintenance-records/`——**开工前必读**，已裁定锚点不得翻案。

## 流程

### 1. 按章分派子 agent

- `Y/<卷>/OEBPS/Text/` 的正文文件（Prologue/ChapterN/Epilogue/Afterwords）每章一个子 agent；<6k 字符的小章合并到一个任务。包装页（Cover/Illustrations/Information 等）不读。
- 提示词模板 `templates/chapter-agent-prompt.md`：替换 `{{VOL_PREFIX}}` 与章节路径后作 `delegate_task` 的 context/goal，模板尾部附 output_schema。一次调用并行分派全部章（S1_01：6 agent / 31 分钟 / 56 发现）。
- 子 agent 回原文核对用 `scripts/proofreading_locate.py`（模板已内置用法）。

### 2. 三层复核（子 agent 产出是自报，不复核不落规则）

1. **机械验证**：`uv run python scripts/agent_proofread/verify_findings.py <卷> findings.json`——中文引文逐字验证（剥标签容差）、日文依据语料验证、X/Y 比对。
2. **X≠Y 归因**：`x_same=false` 的条目查 rules/——良性归一化（如 塑胶→塑料）按上游问题处理；**规则改坏的是规则回归**（案例：`_common` 安心感→安全感 把 X 的正确译法改错），落分卷规则压回，不回改通用规则。
3. **人工判定**：误译高置信条目逐条回原文终判；警惕**模式性误报**——单章看似衍字、全卷实是统一体例（案例：四章标题均带人称「她/他」，单报一章即误报）。

### 3. 落规则

- 候选规则写 TSV（`旧<TAB>新<TAB>注释` 三列，注释含原文依据；旧串为 X 侧 xhtml 逐字，ruby 标签内联）。
- `uv run python scripts/agent_proofread/apply_rules.py <卷> candidates.tsv [--replace 旧串]...`：归属计数（全语料命中须全在本卷，否则报错交人裁决）、正则守卫、双模拟，全过才写 TSV。
- 重跑 `uv run python scripts/sync/x2y.py`，`--verify-y` 终验；`git diff` 应为纯行内替换（+N/−N 相等）。
- rules/ 与 Y/ 同一 commit：`fix: 校正 <卷标识>（N 处文本修订）`，body 附全量 `旧→新`。

## 经验

- 子 agent 引文跨 ruby 标签或跨行：raw 不匹配≠编造，verify_findings 已含容差；仍不符才算编造。
- 既有规则可能与新规则抢同一文本：长规则取代时用 `--replace` 删旧短规则（案例：估计大概→估计）；分卷先于通用应用，顺序即真理。
- TSV 表头必须保留——丢表头 x2y 校验直接拒收（本流程犯过）。
- 子 agent 自报的「读了多少字/查了多少词」不可信，以脚本验证为准。
- 成本参考（S1_01）：11 万字符卷 56 发现、误报 <15%；子 agent 原文回查接近遇疑必查（全卷 164 词），订阅额度按百万 token/卷估。
