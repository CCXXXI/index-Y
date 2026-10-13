---
name: rule-jp-audit
description: "Use when 规则日文依据核查：index-jp 资源更新后，全量生效规则回当前原文核查。"
---

# 规则日文依据核查

index-jp 资源多次更新（BW 替换、逐卷修正）后，部分规则的注释引文与判定前提基于旧版原文。对**全部生效规则**（旧串在 X 语料有命中；失活规则自动排除）回当前 index-jp 逐条核查。

## 流程

1. **生成单元**：`uv run python scripts/rule_jp_audit.py units --out <工作目录>`——每规则采样 ≤3 命中行（优先对齐卷/正文 xhtml/卷间分散），对齐卷（bw_aligned pass，69/81）经 `jp_align.jp_at` 行号直读当前日文行；注释「」引文对当前全语料逐字核验（剥 rt + rt 内联双变体）。输出 units.json + 每 60 条一批的 batch_NN.json。
2. **子代理判定**：一批一子代理（delegate_task 全并行），子代理只 read_file 批次文件判定，output_schema 强制 `{verdicts:[{id,verdict,reason,jp_evidence}]}`。verdict：ok / wrong / stale_evidence / uncertain。**单元 1.9MB 不能抄进调用**——写文件让子代理读（防 context 塌缩事故，同 agent-proofread 教训）。
3. **机械复核**：`uv run python scripts/rule_jp_audit.py verify verdicts.json --units <units.json>`——覆盖度对账 + wrong/stale 的 jp_evidence 逐字验证（防编造）。合并子代理产出从 state.db `async_delegations.result_json` 提取（live transcript 会截断）。
4. **主代理终判**：wrong/uncertain 逐条回原文（X 全命中行 + 对齐 jp ±4 行 + 必要时 `git grep <旧引文> 82b9c4f^` 旧版归因）；stale 刷新注释引文（规则不动）。
5. **落地**：rules/ 改动后重跑 `scripts/sync/x2y.py`，git diff 应为纯行内替换（+N/−N 相等），按改动点数对账；rules/ 与 EPUB/ 同一 commit。

## 判定口径

- **撤删**（前提失效）：现版原文支持「旧」不支持「新」——如 S1_01 奇术师案（现版 奇術師は終焉を与える，旧资源作 魔術師，git 旧 ref 实证）。
- **张冠李戴**：注释引文真实存在但属于另一行，本行 X 忠实——撤规则；原意图若确有缺陷，在正确行另立规则。
- **误命中**：通用规则跨卷绞杀正用（采出/开采出、反应在/金属反应在飞舞、公车/社用車）——缩窄正则（lookahead）或收缩为分卷规则。
- **原文内部不一致**（如 S3_05 搀扶案：L167 上条借肩给白井 vs L186/312 肩を貸している白井黒子）：以本行逐字为准，X 逐行忠实即撤规则，不以场景逻辑翻案。

## 经验

- 引文核验的弱匹配（剥汉字前缀落假名锚点）会把真变化洗白（魔術師→奇術師 也能靠尾部「は終焉を与える」假通过）——quoted 引文只许严格逐字 + 括号/ruby 表示差异变体；注释混排提及词（bare）另列覆盖度，不作判据。
- 子代理的判定依据文档路径：index-X 是 **index-Y 的 submodule**（`index-Y/index-X/docs/...`），不是 `../index-X` 同级目录——写错路径子代理会报「仓库不存在」空转。
- 豁免卷（index-jp `no_original.txt`）命中点标 no_original，按注释与中文侧依据判定，拿不准 uncertain 交主代理。
- 判定要求子代理只引用单元提供的 jp 行（verify 会逐字卡），禁止凭记忆引外部文本。
