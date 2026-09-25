---
name: sync-triage
description: "Use when 同步上游：index-X 推进 pin 起新轮并分流提交。"
---

# 上游同步分流操作手册

本文记录「index-X checkout 新 ref + 重跑 x2y.py 后」把改动审查、入仓的完整方法。

**全流程已固化为脚本，一键执行（任一步失败即中止；各步幂等可整体重跑）：**

```bash
# ===== 起新轮：index-X checkout 上游 ref（自动跑 update_x）+ 导出审查材料 =====
uv run python scripts/sync/run_all.py --sync        # 缺省对齐最新 release tag；--sync <REF> 指定
# 要求工作区干净且 submodule HEAD == pin：上轮未收尾会被拒绝（在途审查会被新版顶掉）——
# 先 --finish 收尾；确认放弃在途审查要强行并入（跨轮残留安全，见下）时，
# 手动 uv run python scripts/sync/update_x.py [--ref REF]
# 起新轮还会先校验 HEAD 自洽（Y == x2y(X)），拦「上轮改 rules/ 后漏跑
# x2y.py」的陈旧 Y——被拒时重跑 x2y.py 并把 Y 侧规则效果提交，再重新起新轮

# ===== 在途重跑（不带 --sync；人工审查前） =====
uv run python scripts/sync/run_all.py

# ===== 人工审查 =====
# 审查 .triage/review_changes.txt（Y 侧文本块 diff 聚合，★ 行为上游 commit
# 归属与记录命中预注），
# 为可疑改动写 x2y 规则（校正或回钉，见「上游错误的规则化」），
# 重跑 uv run python scripts/sync/x2y.py；
# 需暂缓提交的 rel 写入挂起清单 .triage/hold.txt（见「挂起清单」）

# ===== 审查后收尾（自动，工作区清零） =====
uv run python scripts/sync/run_all.py --finish
# 结束后 pin 推进到新 ref（X 与上游逐字一致）、Y == x2y(X)，无任何改动残留在工作区
```

`run_all.py` 依次调用的单步脚本（调试/单步重跑时用）：

```bash
uv run python scripts/sync/check_y_freshness.py      # 前置校验：Y == x2y(X)
uv run python scripts/sync/triage_text.py            # 导出审查材料；--commit 覆盖核验 + 原子提交
uv run python scripts/sync/upstream_context.py       # 上游上下文导出 + records 命中预注（--fetch 起新轮）
# --finish 等价于：check_y_freshness → triage_text --commit
```

脚本间共享状态在仓库根目录 `.triage/`（已 gitignore），其中脚本自管的状态文件只有
`hold.txt`、`review_changes.txt`、`upstream_context.txt`、`sync_ref`，其余均为审查草稿。

**数据模型**：`index-X/` 是上游仓库的 submodule（pin = superproject HEAD 的 gitlink，
即上轮同步点），X 语料即其 `EPUB/` 树。X 侧「旧」= pin，「新」= submodule HEAD，轮内恒定。

**审查材料按构造只有上游驱动改动**：校对提交的 rules/ 与 Y 侧规则效果同 commit 入仓
（HEAD 自洽），轮末 freshness 门挡陈旧/直接编辑的 Y——因此 `review_changes.txt` =
Y 侧 HEAD→工作区的文本块 diff 就是全部待审内容，不做分类、无需 X/Y 配对。审查即门控。
以下改动天然不进审查材料（文本块提取不可见或机械过滤），随原子提交自动入仓：

- 纯格式化/属性/版式改动（文本块零差异的文本文件）；
- 非文本改动（css/图片/字体等，经两侧逐字节镜像校验）；
- opf 的 `dcterms:modified` 时间戳块（机械滤除）；
- 新增/删除文件（上游增删章节，内容审查属通读校对而非同步审查）；
- xhtml rename：纯移动文本零差异（随原子提交），捆绑的文本改动照常进材料。

**上轮分流未完成就同步新改动（跨轮残留）是安全的**：triage_text 每次运行都从实时
工作区重新提取并覆写审查材料（从不读旧值）。防护：默认模式导出材料时把 `.triage/`
内其余散文件与审查目录（`verdicts/`、`review_chunks/`、`review_prompts/`；上轮审查
草稿）轮转入 `.triage/prev/`（仅保留一轮），防止旧 verdict 被误当本轮结论；
`--commit` 模式不清草稿——中止时审查仍在继续，草稿还有用。

## 分流规则

终态模型：**X 是上游逐字镜像（index-X@pin 的 EPUB/ 树），Y 是 `x2y(X)` 净化产物；
每次分流结束工作区必须清零。**

1. **正常同步**：Y 侧改动看起来正常 → 随原子提交入仓。
2. **疑似上游错误**：确认后写成 x2y 规则（能修正写**校正规则**，有证据但无法修正写
   **回钉规则**把该句钉回旧译文），重跑 `scripts/sync/x2y.py` 后 Y 即净化。X 侧照常
   接收上游原文，Y 侧为规则净化后的文本。判 suspect 与写规则都以可坐实的积极证据为
   前提（宁漏报勿误报：宁可放行上游错误改动，不错误指控上游正确改动；「新旧难辨优劣」
   不构成证据，放行入仓）。
3. **规则生命周期**：失活规则保留不删（X 持续增长，错误类规则可能在新内容上复发，
   删除会把未来的漏校正变成人工审查成本，保留的扫描开销可忽略）。同步流程不产出
   失活报告；向上游反馈规则生效状态时用 `scripts/active_rules.py` 现场实测。

## 提交形态

一轮 = 审查期若干 rule commits（rules/ 与其 Y 侧规则效果）+ 轮末一笔原子提交
`{gitlink 推进, 全部 Y 侧改动}`（排除挂起清单）。原子提交命名
`fix: sync X/Y ← index-X@<tag>（N 文件，M 处文本修订）`，body 列逐文件摘要。
X 是上游逐字镜像（含已鉴定并写规则的缺陷文本），Y 是净化产物；
`check_y_freshness`（--finish 前置）保证不变式对全部文件成立。

## 0. 前置校验：Y 必须与当前 X 同步

**分流前先确认 Y 是由当前 X 生成的**，否则审查材料系统性失真（典型原因：忘记重跑
x2y.py，Y 滞后一个上游版本）。

- 症状：改动块异常多；抽查发现 Y 缺上游文本修订。
- 根因：x2y.py 的输出 = `copytree(X)` + fixes，X 若在跑完后又更新，Y 即滞后。
- 验证：`check_y_freshness.py` 用 `regex` 模块（规则含 `\p{}`，stdlib `re` 不支持）
  复算 `fixed(vol, X内容)` 与 Y 文件对比。
- 处理：`uv run python scripts/sync/x2y.py` 重跑后重新走全流程。

## 0.5 分流中途修改规则

分流进行中（工作区有在途改动时）改规则，有触发的规则会使 Y 滞后。用 stash 隔离后落地：

1. `git stash push -u`（必须 `-u`，在途可能含 untracked 新文件）；
2. 改 `rules/`，重跑 `uv run python scripts/sync/x2y.py`；
3. 验证 Y 侧 diff 的每个 hunk 都是规则效果，把规则与 Y 侧改动作为单个 commit 提交；
4. `git stash pop`（同一文件内规则效果与在途改动不重叠时自动合并）；
5. `check_y_freshness` 通过后方可继续；重跑 triage_text 重新导出材料（幂等）。

捷径：若新规则的键在全卷 HEAD X 命中 0 次（回钉/校正上游新增文本的键天然满足），
HEAD 自洽不受影响，可不走 stash：直接重跑 x2y，规则单独成 commit
（`git commit rules/<卷>.tsv -m ...` 带路径限定），Y 侧规则效果随原子提交。

## 1. 通用 git 操作坑（本仓库路径含中文与方括号）

- **方括号路径**：`[S1_01]...` 会被当作 glob 字符类。所有涉及路径参数的 git 命令必须设环境变量 `GIT_LITERAL_PATHSPECS=1`。
- **NUL 分隔**：路径含空格/中文，解析 `git status --porcelain`、`--name-status` 等输出一律加 `-z`（非 z 模式下带空格路径被引号包裹、quotepath 下非 ASCII 被八进制转义，按行解析会静默失配）。rename 的两个路径顺序**因命令而异**：`git status --porcelain -z` 反转为 `to\0from`（省略 `->`），而 `git diff --name-status -z` 与非 z 相同为 `from\0to`（git 2.55 实测，勿凭文档记忆推断）。
- **批量读 git 对象**：逐个 `git show HEAD:path` 起上千个子进程很慢；改用 `git ls-tree -r -z HEAD` 取 path→sha 映射，再挂一个持久的 `git cat-file --batch` 进程按需读内容。
- **带 add 副作用的助手**：`triage_text.y_status()` 自带 `git add -A`（rename 检测需要暂存口径）；`git reset` 清空索引之后不得再调它，否则刚清掉的暂存（含挂起文件）全部回归、被裸 commit 卷进去。
- **按路径提交**：分流在途期间索引长期保持大量暂存，任何提交（含文档/脚本这类无关提交）都必须带路径限定（`git commit -m msg -- <paths>`），否则裸 `git commit` 会把全部在途暂存卷进去。工作区恢复同理：`git checkout -- <path>` 从索引恢复（拿到的是已暂存的改动），要从 HEAD 恢复用 `git checkout HEAD -- <path>`。
- **Python 侧路径匹配**：`glob` 会把卷目录名的 `[S1_01]` 当字符类，静默匹配 0 个文件（检索假阴性）；遍历用 `os.listdir`/`os.walk` 或先 `glob.escape`。
- **Python 生成清单喂 bash 循环**：Windows 文本模式写出 `\r\n`，`while read` 读入的路径带尾 `\r`，git 路径匹配全部失败；先 `tr -d '\r'`，或全程在 Python 内处理。
- **cwd 漂移**：terminal 会话的 cwd 跨调用保持，`cd` 进相邻仓库后 `git show HEAD:EPUB/...` 会作用在错误仓库上（路径不存在时返回该仓库 HEAD 的提交信息而非报错，是假阳性来源）；跑仓库命令前先确认 cwd。Hermes 文件工具的相对路径同样跟随 terminal 实时 cwd。

## 2. 审查材料与审查

`review_changes.txt`：Y 侧文本块 diff 按块聚合（相同改动跨文件去重为 `[N次]`），
每块 `- 旧` / `+ 新` 两行（结构改动为块组多行），块尾 `★` 行是预注（上游 commit
归属 + 上游记录命中，详见「上游记录的使用纪律」）。
写规则需要 X 侧原文时直接在 `index-X/EPUB/<卷>/` 检索片段。

### 上游错误的规则化

可疑块写成 x2y 规则（分卷 TSV），重跑 `x2y.py` 后 Y 即净化、块从材料消失：

- **校正规则**（缺陷文本 → 修正文本）：确认是上游错误且能给出修正时使用；有原文对照的规则在 TSV 第三列（注释列）附原文引文与理由。
- **回钉规则**（新文本 → 旧文本）：有积极证据表明新译有误、但无法给出修正时使用，把该句钉在旧译文上（「存疑」指有具体疑点支撑，如无原文坐实的新旧难辨不适用——放行入仓不写规则）。上游日后再改这句时规则失配，该句以新形态重新进入审查。
- 规则键要求：
  - 键在全卷 X 中的命中数必须恰为预期（防误伤，短键务必实测计数）。
  - 回钉/插入类规则的键不得命中旧文本（否则改写既有正确文本）；把键加长到只匹配新文本。
  - 默认写分卷规则限定作用域；跨卷通用的高置信缺陷才进 `_common.tsv`。

### 正常性判断

上游同步的文本改动绝大多数是：术语统一、错别字、的地得、标点全半角、人名译名统一、以及整段流畅的重译。这些都算正常。

需要**剔除留审**的异常特征。上游的错误改动一般是**批量替换未经充分检查**的机械产物；整卷的「重译波」同样可能是批量机械产物，不因其文风统一而豁免逐块判定：

- 明显退化（错别字、的地误用方向反了等）；
- 重复字词/标点、混入杂字符；
- 删字导致病句、句子成分残缺；
- 无法从翻译角度解释的名词/术语替换；
- 数字、专名改动且无规律可循。

语义/措辞类改动（含整句重译、含义反转、语序调整）一律对照日文原文核实后再判——通顺不等于正确，看似莫名的改动也可能是原文的忠实再现。**对照核实的流程、判定细则与例外见 index-jp**（私有仓库，读取时其 AGENTS.md 自动注入）。人名/译名统一类改动先查 Y 语料既定惯例确认统一方向，统一成错误形式即退化。

- 术语统一波次先查上游有无新增译注说明政策（命中不全 ≠ 批量误替，可能是按政策的语境区分；带 ruby 注音的术语在文本块提取中是独立块，按基底+注音结构核对命中范围与未命中原因）；统一方向的判定看目标形式是否为错误形式（原文既定形式或语料多数形式任一成立即非退化），不看改前是否为多数。
- `Note.xhtml` 译注的新增/调整是翻译团队自加内容，无原文可对照，按事实准确性审查。
- 插入块可用上下文自洽性实证：若后文存在引用该插入内容的悬空指代（旧文本中无先行词），插入即补译而非杜撰。

难以判断的一律保留审查；处分（写规则）以可坐实的积极证据为前提，宁漏报勿误报。

### 上游记录（index-X submodule）的使用纪律

`index-X/` submodule 是审查参考源（仅经 git plumbing 读，不依赖其工作区状态）。`upstream_context.py`（run_all 自动调用，起新轮带 `--fetch`）做两层机械工作：

- **pin 区间列举**：基准 = superproject HEAD 的 gitlink（上轮同步点），新侧 = submodule HEAD（本轮 checkout 的 ref），区间精确到 commit。导出区间内逐卷新 commit 列表（含 body）与 maintenance-records 全文至 `.triage/upstream_context.txt`；origin/master 领先本轮同步点时附注（属下轮内容）。
- **块级预注**：两类 `★` 前缀行插入审查材料块尾（prepare_review 解析时跳过）：
  - `★ commit <sha>: <subject>`：块的引入 commit（块文本在区间 commit 的 diff
    纯文本中整段逐字命中，时间序；短块要求双侧命中同一 commit，整句逐字巧合
    概率可忽略故无需统计门槛）。2+ 个 = 振荡链（旧文命中首改、新文命中末改）/
    多区域段落块/聚合块拆分波次。`未定位` = 无逐字命中（1 字块、ruby 隔断、
    规则交叠等）——宁可空缺不错指。body 在 `upstream_context.txt` 区间列表查。
  - `★ <record>: <行>`：改动点子串在全部 records 中检索的命中行。

记录（commit message 与 maintenance-records）是上游的**自报证据，不免检**。使用分四层：

1. 记录描述的 `旧→新` 与实际 diff 机械一致（预注命中本身即一层佐证）；
2. 所引日文原文真实存在——程序化 grep 核验；
3. 引文是否真支持改动方向、统一方向裁定是否合理；
4. 无记录覆盖的块（如整卷重译波）走原有 JP 核实路径，不变。

记录的检索价值：把语义块的「开放式 JP 检索」降级为「按引文确认」；但读完 `upstream_context.txt` 与 ★ 预注后仍按「正常性判断」逐块判定。

### 大规模 AI 审查（子代理流程）

候选块多、语义改动占比大时（如整卷重译波），派子代理分片对照日文原文审查：

```bash
uv run python scripts/sync/prepare_review.py   # 解析材料 → jp_text/ 原文纯文本 + review_chunks/ 任务块 + review_prompts/ 提示词
# delegate_task 每个 task_NN.md 派一个子代理（中断可安全重派）
uv run python scripts/sync/aggregate_verdicts.py  # 校验完整性 + jp 引用逐字核验 + 导出 suspect/unsure/unlocated
```

- 判定词汇（ok/suspect/unsure/unlocated）与原文检索纪律的权威版本在 prepare_review.py 的提示词模板内。缺原文卷由 prepare 机械消费 ../index-jp/no_original.txt（无原文豁免卷清单）后分流：清单内卷豁免对照（任务提示词附豁免判定口径：按语料惯例与流畅度判定、jp 留空），其余缺卷以 `index-X/EPUB/` 下完整目录名报用户补充（照抄目录名，不凭编号回忆卷名；新增豁免登记 no_original.txt——它是豁免清单的唯一权威）。
- 兼容规范化块（新旧文本 NFKC 等价，差异仅来自全角/半角等兼容字符形式，如 ＆→&）由 prepare_review 自动判 ok 写入 `verdicts/auto.jsonl`，不进任务块。其余块按 diff 片段形态聚合：≥3 块同形态（同一批量替换波）整簇成一个任务（不拆分），任务 JSON 带 clusters 字段、提示词附簇判定口径（代表块严格对照、成员逐块确认语境、同簇证据共享；像/象类语境敏感替换仍逐块判）。形态 Top 与逐任务簇规模打印供抽查。
- 子代理 verdict 是自报结论：suspect/unsure/unlocated 逐条人工复核、ok 抽查后，再放行或写规则。
- review_blocks.json 的块 id 是 verdict 的关联键。verdicts/ 非空时 prepare_review 拒绝重建（防在途任务被换底）——triage_text 重新导出材料时已把上轮 `verdicts/`、`review_chunks/`、`review_prompts/` 轮转入 prev/，故拒绝即本轮真在途：先聚合存档本轮 verdict，清空 verdicts/ 再重建。

## 3. 挂起清单

需暂缓提交的 rel 写入 `.triage/hold.txt`（每行 `相对路径（不带 EPUB/ 前缀）<TAB>理由`，
`#` 开头为注释）：该 rel 的块不进审查材料，`--commit` 跳过其 Y 侧提交（X 侧随 pin
全量入仓）。失效条目（Y 侧无在途改动）每轮自动剔除。`--commit` 后仅剩挂起改动时
单列报告并以非零码退出（轮次保持开放，下一轮 `--sync` 仍被 update_x 拒绝）；
核实完成后从清单删行重跑 `--finish`（Y 侧改动经 freshness 验证后直接随原子提交
入仓），直至输出「工作区已清零」。

## 4. 收尾

triage_text `--commit` 先做覆盖核验：Y 侧每条在途改动都必须入覆盖集（文本块/新增/
删除/纯格式化/非文本镜像/挂起），非 Y 侧改动（rules/、scripts/ 等 stray）同样列出——
**核验先于提交，中止时本轮不产生任何提交**，人工按性质提交后重跑 `--finish`。
核验通过后一笔原子提交 `{gitlink 推进, 全部 Y 侧改动}`，工作区清零后分流结束。
