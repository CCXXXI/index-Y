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

- `uv run python scripts/agent_proofread/plan_chunks.py <卷>`：把整卷正文切成 ≤10 块（delegate_task 并行上限），每块 ~1.5 万字符（大卷自动加大），主区间无缝拼接、前后 ~10 行重叠上下文。包装页（Cover/Illustrations/Information 等）自动跳过。小块试验直接给参数：`--target-chars 8000 --max-chunks 16`。
- 分派参数以 plan_chunks 输出为准直接复制：goal 用逐块 goal 行，context 与 output_schema 用末尾印出的全块共用版（context 是短指令，让子 agent 自 skill_view 加载 `templates/chapter-agent-prompt.md` 并替换 `{{VOL_PREFIX}}`）。**禁止把 4.5KB 模板全文 ×N 块手工抄进调用**——长重复 JSON 抄写会塌缩漏字段（S1_04 事故：context 连续 4 次漏写，steer 才救回）。一次调用并行分派全部块。
- 子 agent 回原文核对用 `scripts/proofreading_locate.py`，模板要求**批量一次跑完**（试跑教训：一词一查会让单 agent 拖到 40+ 轮）。
- 时效目标：**单块 <15 分钟**——理由是整卷 makespan = 最慢块（分析轮随块字符增长，S1_01 按章分派时 3.8 万字大章单 agent 跑了 30 分钟）。缓存不是依据：TTL 随轮刷新，活跃子会话不会全价重付；>20 分钟的真实代价只是结果回注主代理时主上下文冷读一次，量小。

### 2. 三层复核（子 agent 产出是自报，不复核不落规则）

1. **机械验证**：把各子 agent 返回的 findings 数组合并为一个 JSON 文件（保留 file/line/quote/jp_evidence 等字段），跑 `uv run python scripts/agent_proofread/verify_findings.py <卷> findings.json`——中文引文逐字验证（剥标签容差）、日文依据语料验证、X/Y 比对。块间重叠上下文行若被多个子 agent 重复上报，按 (file, line) 去重。
2. **X≠Y 归因**：`x_same=false` 的条目查 rules/——良性归一化（如 塑胶→塑料）按上游问题处理；**规则改坏的是规则回归**（案例：`_common` 安心感→安全感 把 X 的正确译法改错），落分卷规则压回，不回改通用规则。
3. **人工判定**：误译高置信条目逐条回原文终判；警惕**模式性误报**——单章看似衍字、全卷实是统一体例（案例：四章标题均带人称「她/他」，单报一章即误报）。**无法确定即不采**：本流程不留「存疑交人工」，agent 判定不了的条目按不采忽略并在 commit body 登记理由（含双关改写方案、体例口径冲突、首现写法存疑等）。

### 3. 落规则

- 候选规则写 TSV（`旧<TAB>新<TAB>注释` 三列，注释含原文依据；旧串为 X 侧 xhtml 逐字，ruby 标签内联）。
- `uv run python scripts/agent_proofread/apply_rules.py <卷> candidates.tsv [--replace 旧串]...`：归属计数（全语料命中须全在本卷，否则报错交人裁决）、正则守卫、双模拟，全过才写 TSV。
- 重跑 `uv run python scripts/sync/x2y.py`，`--verify-y` 终验；`git diff` 应为纯行内替换（+N/−N 相等）。
- rules/ 与 Y/ 同一 commit：`fix: 校正 <卷标识>（N 处文本修订）`，body 附全量 `旧→新`。
- **登记 `status.md`**：更新该卷「本项目」列为日期（提交号，修订处数），随本批改动一起提交。

## 经验

- 子 agent 引文跨 ruby 标签或跨行：raw 不匹配≠编造，verify_findings 已含容差；仍不符才算编造。
- 原文仓库部分文件的读音写在 `<rt>` 之外（如 安堵あんど）：verify_findings 已用读音容忍正则兜底（仅兜底救回的条目打 jp_flex 标记）；仍 jp_ok=False 的是拼接/改写/引号差异等非逐字引文，取 locator 原文命中人工核一眼再判，勿直接判编造。
- 既有规则可能与新规则抢同一文本：长规则取代时用 `--replace` 删旧短规则（案例：估计大概→估计）；分卷先于通用应用，顺序即真理。
- TSV 表头必须保留——丢表头 x2y 校验直接拒收（本流程犯过）。
- 子 agent 自报的「读了多少字/查了多少词」不可信，以脚本验证为准。
- 各脚本的卷参数按目录名 startswith 匹配，须带方括号前缀（`[S1_02`，裸 `S1_02` 匹配 0 个目录）。
- 块尺寸再认识（S1_03 task-3 转录实证）：墙钟大头是单块「通读分析」那一轮（14 分钟/390 行，约占半）与 jp 补查轮，两者都随块缩小而减；并行上限是 `delegation.max_concurrent_children` 配置项而非硬约束。更多更小的块（~7-8k）墙钟很可能改善（下限是每块 ~5-8 分钟固定地板：读取/体例核查/定位器/终报），长上下文降质方向合理且有实例（16k 块漏掉区间内七大词簇，复核层兜底抓获）。下卷试小块+调高并行，用 verify 数据对比发现率再定档。
- 子 agent 低效模式（模板可防）：①search_files 第一把拿 count 格式又重跑拿 content，白烧一轮——提示词应指定带内容的输出；②定位器清单没收全候选，转用裸 grep 进 index-jp 逐文件翻（14 轮摸结构+grep）——回查词宁滥勿缺全进清单，补查也用定位器第二轮，不裸 grep。
- 候选规则的新串要按「管道终态」写：遇带上下文条件的通用规则（如 `_common` 152 `由于(?=…所以)→因为`、154 删「的关系」），新串若仍含其触发模式，通用规则会在分卷规则之后再改一遍，双模拟终态断言必失败——把触发上下文（但/所以、因…的关系）纳入旧串并在新串直写终态（案例：但由于…流动，所以→但因为…方向，所以；因角度的关系→因角度关系）。
- delegate_task 分派事故（S1_04 犯过）：主代理连续多次调用漏写 tasks 项的 `context`（响应回显只有 goals，发现不了），子 agent 裸 goal 起跑。根因是手工抄写 15×4.5KB 重复 JSON 致字段塌缩；根治是把机械抄传输收进脚本——context 短指令化（子 agent 自载模板）+ plan_chunks 直印 goal/context/output_schema 三参数（见流程 §1）。「分派后读一只 live transcript 的 kickoff 验证」仍是兜底探测；补救用 `delegate_task(action='steer')` 逐只补发指令可行（子 agent 多会自行 skill_view 模板但不可依赖）。
- verify_findings 大面积 jp_ok=False 先怀疑引用 artifact 而非编造：定位器输出的（partXXXX.html）来源标注、多句「／」拼接、中文解说混进 jp_evidence 都会判负（模板已补「jp_evidence 只写日文句子本身」）。复核姿势：剥标注、按 ／ 切段、core 子串逐条探测（S1_04：49 条 jp 证据初验仅 6 过，清洗后全部坐实，0 编造）。
- 子 agent final answer 在 live transcript 里被截断（~625 字符）；合并 findings 从 `state.db` 的 `async_delegations.result_json` 提取完整 summary，勿手抄。
- 成本参考（S1_01，按章分派旧方案）：11 万字符卷 56 发现、误报 <15%、6 agent 31 分钟；子 agent 原文回查接近遇疑必查（全卷 164 词），订阅额度按百万 token/卷估。切块 + 批量查询后墙钟应压到 ~15 分钟级。S1_02 实测（切块新方案）：12 万字符卷 9 块并行，最慢块 16.6 分钟，54 发现采纳 55 条规则（57 处修订）、否决 1 条。S1_03 实测：11.3 万字符卷 8 块并行，最慢块 29 分钟（发现密度最高块：14 分钟单轮通读分析 + 7.5 分钟 jp 补查），54 发现采纳 71 条规则（70 处修订）、不采 4 条。S1_04 实测（8k 小块 ×15 全并行）：11.3 万字符卷 makespan 29.8 分钟（最慢块 3 轮清单 35 词、21 次 API），54 发现采纳 47 条规则（48 行修订）、不采 7（低置信 4 + 无法确定 3）——小块方案 makespan 未缩短（与 S1_03 持平），墙钟变量是清单轮数×词数而非单块字符；分派事故损失约 4 分钟。
