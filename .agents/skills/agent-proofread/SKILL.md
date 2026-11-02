---
name: agent-proofread
description: "Use when agent 通读校对：按章分派子 agent 通读整卷 Y 产物，发现、复核并落规则。"
---

# agent 通读校对

对上游已完成全卷校对、但本项目尚未校对的卷做二校：子 agent 按块通读 Y 产物，判定口径与规则落地同 proofreading。**开工选卷先查 `status.md`（各卷 AI 校对状态；文件在本 skill 目录下，即 `.agents/skills/agent-proofread/status.md`，不在仓库根）。**

## 判定依据（指针，不复制进本仓库）

- 规范正文：`index-X/docs/translation-spec.md`——尤其「四、语义与信息」（哪些必判缺陷）与「六、证据边界」（防误报）。
- 已裁定索引：`index-X/.agents/skills/translation-term-unification/SKILL.md` §六 + `index-X/docs/maintenance-records/`——**开工前必读**，已裁定锚点不得翻案。

## 流程

### 1. 切块分派子 agent（全文对照）

- `uv run python scripts/agent_proofread/plan_chunks.py <卷>`：默认把整卷正文切成不超过 10 块（软上限，可用 `--max-chunks` 调整），主区间无缝拼接、前后 ~10 行重叠上下文。包装页（Cover/Illustrations/Information 等）自动跳过。
- **中日逐行对照文件**（bw_aligned pass 卷）：plan_chunks 为每块生成 `.triage/proofread_chunks/<卷码>/chunk-NN.txt`——每物理行一对 CN（Y 剥标签，ruby 注音连写）/JP（对齐单元同行剥标签），行号即 Y 物理行号（`check_y_jp_alignment` 门禁保证行结构一致）。派生产物，每轮开工重新生成；对齐缺失/行数不等的单元（PAIR_RULES 豁免等）段内只列 CN 并标注「※无行号对齐」；非 pass 卷自动退回旧形态（goal 直指 xhtml 行区间 + locator 核对，模板【无行号对齐卷模式】节）。块尺寸默认 1.2 万中文字符/块（对照形态输入近翻倍；未对齐卷仍 1.5 万）；大卷（>12 万字符）给 `--max-chunks 14` 维持块尺寸。并行上限读 `delegation.max_concurrent_children` 当前配置。
- 分派参数以 plan_chunks 输出为准直接复制：goal 用逐块 goal 行，context 与 output_schema 用末尾印出的全块共用版（context 是短指令，让子 agent 自 skill_view 加载 `templates/chapter-agent-prompt.md` 并替换 `{{VOL_PREFIX}}`）。禁止把模板全文逐块手工抄进调用。一次调用并行分派全部块。
- 子 agent 的日文依据随对照文件下发（JP 行照抄进 jp_evidence，误译/漏译类必附）；`scripts/proofreading_locate.py` 只剩两用途：「不一致」类的全卷计数、无对齐段/失配区/非对齐卷兜底。回查词清单一次收全并批量运行，补查也用定位器，不裸 grep 逐文件翻。
- 块尺寸与额度：按发现质量、当前模型与相近流程的实测消耗及 5h 窗余量定档，不按墙钟。块越大单块分析轮越长；无可比实测时先小批试点，预期超窗就切细或跨窗。缓存 TTL 随轮刷新，活跃子会话不会全价重付。

### 2. 三层复核（子 agent 产出是自报，不复核不落规则）

1. **机械验证**：把各子 agent 返回的 findings 数组合并为一个 JSON 文件（保留 file/line/quote/jp_evidence 等字段），跑 `uv run python scripts/agent_proofread/verify_findings.py <卷> findings.json`——中文引文逐字验证（剥标签容差）、日文依据语料验证、X/Y 比对。块间重叠上下文行若被多个子 agent 重复上报，按 (file, line, category, quote) 去重。
2. **X≠Y 归因**：`x_same=false` 的条目查 rules/——良性归一化按上游问题处理；**规则改坏的是规则回归**，落分卷规则压回，不回改通用规则。
3. **人工判定**：误译高置信条目逐条回原文终判；警惕**模式性误报**——单章看似衍字、全卷实是统一体例时不得按孤例采纳。**无法确定即不采**：本流程不留「存疑交人工」，agent 判定不了的条目按不采忽略并在 commit body 登记理由（含双关改写方案、体例口径冲突、首现写法存疑等）。

### 3. 落规则

- 候选规则写 TSV（`旧<TAB>新<TAB>注释` 三列，注释含原文依据；旧串为 X 侧 xhtml 逐字，ruby 标签内联）。
- `uv run python scripts/agent_proofread/apply_rules.py <卷> candidates.tsv [--replace 旧串]...`：归属计数（全语料命中跨卷即报错交裁决——**裁决口径：默认顺带一并改**，各命中点逐条回原文确认同译无害后提升 _common 或逐卷落规则；仅当别卷命中不宜改时才加长旧串限本卷）、正则守卫、双模拟，全过才写 TSV。
- 重跑 `uv run python scripts/sync/x2y.py`，`--verify-y` 终验；`git diff` 应为纯行内替换（+N/−N 相等）。
- rules/ 与 EPUB/ 同一 commit：`fix: 校正 <卷标识>（N 处文本修订）`，body 附全量 `旧→新`。
- **登记 `status.md`**（本 skill 目录下同名文件）：更新该卷「本项目」列为日期（提交号，修订处数），随本批改动一起提交。

## 复核与排错

- 子 agent 引文跨 ruby 标签或跨行时，raw 不匹配不等于编造；verify_findings 已含剥标签容差，仍不符才按编造处理。
- 对照文件（`.triage/proofread_chunks/`）是派生产物：源为 Y 产物与 bw_aligned 对齐单元，行结构一致由 check_y_jp_alignment 门禁保证；每轮开工重新生成，规则落地后 Y 会变。子 agent 的 quote 是剥标签文本（注音连写形态）；jp_evidence 照抄 JP 行即可过语料验证。发现连续多行 CN/JP 不相关按行号失配处理：该区域退回 locator 兜底并在 stats.note 登记。
- 候选规则的预期命中数与归属报告不一致时，逐处查看命中形态再定旧串。错字/短词规则先全语料排查子串误命中与正用；有正用就带上下文加长旧串。
- 原文仓库部分文件的读音写在 `<rt>` 之外：verify_findings 已用读音容忍正则兜底，仅兜底救回的条目打 jp_flex 标记；仍 jp_ok=False 的是拼接/改写/引号差异等非逐字引文，取 locator 原文命中人工核一眼再判，不直接判编造。
- 既有规则可能与新规则抢同一文本：长规则取代时用 `--replace` 删旧短规则；分卷先于通用应用，顺序即真理。
- TSV 表头必须保留，丢表头会使 x2y 校验拒收。
- 子 agent 自报的「读了多少字/查了多少词」不可信，以脚本验证为准。
- 各脚本的卷参数接受卷目录全名或卷码（如 `S1_02`，全/半角括号形态亦可），须唯一匹配。
- 子 agent 检索要直接取带内容的输出，避免先取 count 再重跑 content；回查词宁滥勿缺，全进定位器清单。
- 候选规则的新串按「管道终态」写：若新串仍含带上下文条件的通用规则触发模式，通用规则会在分卷规则之后再改一遍，导致双模拟终态断言失败。把触发上下文纳入旧串，并在新串直写终态。
- 分派后读一只 live transcript 的 kickoff，核对 goal、context、output_schema 实际到达；漏指令时用 `delegate_task(action='steer')` 逐只补发，不依赖子 agent 自行补全。
- verify_findings 大面积 jp_ok=False 时先检查引用 artifact：定位器输出的来源标注、多句「／」拼接、中文解说混入 jp_evidence 都会判负。jp_evidence 只写日文句子本身；复核时剥标注、按 ／ 切段、逐条探测核心子串。
- 子 agent final answer 在 live transcript 里可能被截断；合并 findings 从 hermes 根目录 `state.db` 的 `async_delegations.result_json` 提取完整 summary（结构 `{results:[{summary}]}`），勿手抄。`shared-state.db` 无此表。
- 合并去重键必须含 category：同一行可报多条不同类发现；用 (file, line, category, quote)，不要只按 (file, line) 去重。
- 一条规则的旧串可在卷内多行命中且双模拟照过：提交前核对 git diff 行数与规则数；多命中的行逐条回原文确认同译无害，再登记进 body。
- 成本归集记录标注模型、块尺寸、任务数与流程形态；按卷归集会话语法见 hermes-usage-stats（parent_session_id 分批 + 批次分钟窗），供后续相近任务估算。
- 判「不一致」前必须回 ../index-jp 查卷内各写法的日文锚点是否本身分层或带区分。已裁定分层口径（如 サイド→阵营、側→侧）不得翻案；原文自带区分即驳回。对照形态下锚点写法就在 JP 行，先看对照行再决定是否查卷内分布。
- 合并 findings 的两个工具坑：①JP-only 关键词进 proofreading_locate 是在 X 中文语料计数，查日文锚点写法直接检索 ../index-jp 对应卷；②子 agent 不遵守 output_schema、散文与 JSON 混排时，从首个 `{` 起用 json.raw_decode 提取，不必打回重跑。
- 校对范围不限本卷：顺带发现的别卷同一问题一并改掉。归属计数报错交裁决时默认走「跨卷一并改」，各命中点逐条回原文确认同译无害后提升 _common 或逐卷落规则；只改本卷是例外。以「系列惯用」为由不采前，先拆解命中构成，排除误命中、专名与其他语境正用。
- 港台用词直接报缺陷落规则，不以「卷内体例一致」为由不采；新添候选先全语料计数、逐条语境排查，并与 _common 既有规则查重。
- 子代理的证据侧只有 Y 与日文原文，X（规则应用前的上游文本）不是证据：定位器固定用 `--side y`（Y 侧计数+Y 上下文+JP）；默认 both 保留给维护者阅读校对。主代理复核层 verify_findings 的 X/Y 比对不受影响。
- 候选 TSV 写盘后先对行数（应等于采纳规则数），规则落盘并渲染后再对 EPUB numstat；两道对账都通过才算完成。
