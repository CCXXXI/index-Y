# 上游同步分流操作手册

本文记录「新 X 入仓 + 重跑 x2y.py 后」把海量改动分流为有序 commits 的完整方法。

**全流程已固化为脚本，一键执行（任一步失败即中止；各步幂等可整体重跑）：**

```bash
# ===== 起新轮：上游 zip 入仓（自动跑 update_x）+ 全自动（人工审查前） =====
uv run python scripts/sync/run_all.py <上游.zip>
# 带 zip 要求工作区干净：上轮未收尾会被拒绝（在途审查会被新版顶掉）——
# 先 --finish 收尾；确认放弃在途审查要强行并入（跨轮残留安全，见下）时，
# 手动 uv run python scripts/sync/update_x.py <zip>
# 带 zip 还会先校验 HEAD 自洽（Y == x2y(X)），拦「上轮改 rules/ 后漏跑
# x2y.py」的陈旧 Y（规则补渲染会当「仅 Y 改动」混进审查）——被拒时重跑
# x2y.py 并把 Y 侧规则效果提交，再重新起新轮

# ===== 在途分流中重跑（不带 zip；人工审查前） =====
uv run python scripts/sync/run_all.py

# ===== 人工审查 =====
# 审查 review_changes.txt / suspect_changes.txt，
# 为可疑改动写 x2y 规则（校正或回钉，见第 6 节），重跑 uv run python scripts/sync/x2y.py；
# 需暂缓提交的 rel 写入挂起清单 .triage/hold.txt（见第 8 节）

# ===== 审查后收尾（自动，工作区清零） =====
uv run python scripts/sync/run_all.py --finish
# 结束后 X 与上游逐字一致、Y == x2y(X)，无任何改动残留在工作区
```

`run_all.py` 依次调用的单步脚本（调试/单步重跑/`--dry-run` 时用）：

```bash
uv run python scripts/sync/check_y_freshness.py      # 前置校验：Y == x2y(X)
uv run python scripts/sync/commit_image_renames.py
uv run python scripts/sync/commit_image_refs.py
uv run python scripts/sync/commit_xhtml_renames.py  # xhtml rename 纯移动提交 + 捆绑修订挂起清单
uv run python scripts/sync/commit_pure_formatting.py
uv run python scripts/sync/commit_layout.py
uv run python scripts/sync/triage_text.py            # 分类 + 导出审查材料；--commit 门控 + 批发提交
uv run python scripts/sync/commit_adopted_x.py       # 导出失活规则候选（批发模型下通常无提交）
uv run python scripts/sync/report_inactive_rules.py  # 报告失活规则（不删除）
# --finish 等价于：check_y_freshness → triage_text --commit → commit_adopted_x → report_inactive_rules
```

图片重命名/引用、xhtml rename、纯格式化/版式各批次支持 `--dry-run`。脚本间共享状态（rename 映射、审查材料）在仓库根目录 `.triage/`（已 gitignore）。

`.triage/` 中脚本自管的状态文件只有 `rename_map.json`、`hold.txt`、`plan.json`、`review_changes.txt`、`suspect_changes.txt`、`adopted_rules.json`，其余均为审查草稿。**上轮分流未完成就同步新改动（跨轮残留）是安全的**：triage_text 每次运行都从实时工作区重新分类并覆写审查材料（从不读旧值）；`adopted_rules.json` 是累积制，候选由 report_inactive_rules 对当前 HEAD 与工作区机械复验后剔除。两个防护：triage_text 默认模式导出材料时把 `.triage/` 内其余文件（上轮审查草稿）轮转入 `.triage/prev/`（仅保留一轮），防止旧 verdict 被误当本轮结论；triage_text `--commit` 工作区清零后删除 `rename_map.json`（映射只服务于轮内 commit_image_renames → commit_image_refs）。`--commit` 模式不清草稿——中止时审查仍在继续，草稿还有用。

各批次无对应改动时脚本自然空跑、不产生 commit，直接顺序往下跑即可。上游重命名图片的情况很少：commit_image_renames 无图片重命名时也会写出空 `rename_map.json`，commit_image_refs 读到空映射直接跳过。

## 分流三类规则

终态模型：**X 是上游逐字镜像，Y 是 `x2y(X)` 净化产物；每次分流结束工作区必须清零。**

1. **正常同步**：X 和 Y 都有某项改动，且改动看起来正常 → 两边批发提交。
2. **疑似上游错误**：确认后写成 x2y 规则（能修正写**校正规则**，存疑写**回钉规则**把该句钉回旧译文），重跑 `scripts/sync/x2y.py` 后随批发提交；X 侧照常接收上游原文，Y 侧为规则净化后的文本。
3. **规则采纳/规则渲染差异**：两侧差异能被规则解释（上游采纳规则、规则因上游编辑获得/失去触发语境、回钉/校正规则生效）→ 随批发提交。report_inactive_rules 机械验证并报告失活规则（保留不删：X 持续增长，错误类规则可能在新内容上复发，删除会把未来的漏校正变成人工审查成本，而保留的扫描开销可忽略）。

版式调整、文件重命名、时间戳等机械性改动一律视为正常同步，无需逐项审查。

## 提交顺序（自动分流先行，人工审查最后）

分批提交，每批提交前程序化校验：

1. 图片重命名（单个 commit）
2. 图片引用更新（opf/xhtml 中的 href/src，单个 commit）
3. xhtml rename 一律纯移动提交（捆绑修订各回各管道，语义改动入挂起清单，commit_xhtml_renames）
4. 纯格式化变更（不影响文本显示，单个 commit）
5. 版式调整（结构/属性变化但文本不变，单个 commit）
6. 文本块分类导出审查材料（triage_text）→ 导出失活规则候选（commit_adopted_x）→ 报告失活规则（report_inactive_rules）
7. 人工审查：正常块放行；可疑块写 x2y 规则（校正或回钉）并重跑 `scripts/sync/x2y.py` → `triage_text.py --commit` 批发提交全部改动（有未处理块即中止；挂起清单 `.triage/hold.txt` 内的 rel 跳过并单列报告），再补跑 commit_adopted_x/report_inactive_rules
8. 工作区清零后分流结束

文本同步提交命名 `fix: sync <相对路径>（N 处文本修订）`，X 侧规则渲染差异的提交命名 `fix: sync X/<相对路径>（N 处文本修订，Y 侧规则渲染）`。

## 0. 前置校验：Y 必须与当前 X 同步

**分流前先确认 Y 是由当前 X 生成的**，否则整个分类会系统性失真（典型原因：忘记重跑 x2y.py，Y 滞后一个上游版本）。

- 症状：`diff` 桶（两侧不一致）异常多；「仅 X 改动」异常多；抽查发现 Y 缺 DOCTYPE/新图名/上游文本修订。
- 根因：x2y.py 的输出 = `copytree(X)` + fixes，X 若在跑完后又更新，Y 即滞后。
- 验证：用 `regex` 模块（规则含 `\p{}`，stdlib `re` 不支持）复算 `fixed(vol, X内容)` 与 Y 文件对比。
- 处理：`uv run python scripts/sync/x2y.py` 重跑后重新走全流程。之前的提交无需重做——各批次的校验逻辑（机械变换逐行验证、改动集完全相等才准入）保证滞后只会造成漏收、不会造成错收。
- 注意：重跑后 Y 树会涌现整批机械改动（图片重命名、格式 wave），按图片/格式化/版式批次同样处理即可。

## 0.5 分流中途修改规则（含捞回历史规则）

分流进行中（工作区有在途改动时）改规则，有触发的规则会使 Y 滞后、HEAD 上 fixed(X) == Y 失效。用 stash 隔离后落地：

1. `git stash push -u`（必须 `-u`，在途可能含 untracked 新文件）；
2. 改 `rules/`，重跑 `uv run python scripts/sync/x2y.py`；
3. 验证 Y 侧 diff 的每个 hunk 都是规则效果，把规则与 Y 侧改动作为单个 commit 提交——这一步让新 HEAD 上 fixed(X) == Y 重新成立；
4. `git stash pop`（同一文件内规则效果与在途改动不重叠时自动合并）；
5. `check_y_freshness` 通过后方可继续分流；若 review 材料已生成，重跑 triage_text 重新导出（幂等）。

捷径：若新规则的键在全卷 HEAD X 命中 0 次（回钉/校正上游新增文本的键天然满足），fixed(X_head) == Y_head 不变式不受影响，可不走 stash：直接重跑 x2y、确认 Y 侧 diff 均为规则效果后，规则单独成 commit，改动块随批发提交。

## 1. 通用 git 操作坑（本仓库路径含中文与方括号）

- **方括号路径**：`[S1_01]...` 会被当作 glob 字符类。所有涉及路径参数的 git 命令必须设环境变量 `GIT_LITERAL_PATHSPECS=1`。
- **NUL 分隔**：路径含空格/中文，解析 `git status --porcelain`、`-–name-status` 等输出一律加 `-z`。rename 的两个路径顺序**因命令而异**：`git status --porcelain -z` 反转为 `to\0from`（省略 `->`），而 `git diff --name-status -z` 与非 z 相同为 `from\0to`（git 2.55 实测，勿凭文档记忆推断）。
- **批量读 git 对象**：逐个 `git show HEAD:path` 起上千个子进程很慢；改用 `git ls-tree -r -z HEAD` 取 path→sha 映射，再挂一个持久的 `git cat-file --batch` 进程按需读内容。
- **构造「部分改动」的暂存版本**（如只提交文本之外的版式变化）：在内存中拼出中间版本内容 → `git hash-object -w --stdin` 写成 blob → `git update-index --cacheinfo <mode>,<sha>,<path>` 更新索引。**工作区文件不动**，剩余改动自动留作未提交。
- **按路径部分提交**：全部改动已暂存时，逐文件提交用 `git commit -m msg -- <paths>`（提交工作区状态，其余暂存项不受影响）。不要用「reset 全部再重新 add」的循环，除非像 rename 那样必须先精确重建索引。**分流在途期间索引长期保持大量暂存，任何提交（含文档/脚本这类无关提交）都必须带路径限定**，否则裸 `git commit` 会把全部在途暂存卷进去。
- **重建索引提交法**（用于 rename/部分版本）：保存目标文件的 `git ls-files -s -z` 条目 → `git reset -q` → `git update-index -z --index-info` 喂回条目 → 校验暂存区恰好是目标集合 → commit → `git add -A` 恢复其余暂存。
- **Python 侧路径匹配**：`glob` 会把卷目录名的 `[S1_01]` 当字符类，静默匹配 0 个文件（检索假阴性）；遍历用 `os.listdir`/`os.walk` 或先 `glob.escape`。
- **Python 生成清单喂 bash 循环**：Windows 文本模式写出 `\r\n`，`while read` 读入的路径带尾 `\r`，git 路径匹配全部失败；先 `tr -d '\r'`，或全程在 Python 内处理。
- **cwd 漂移**：terminal 会话的 cwd 跨调用保持，`cd` 进相邻仓库后 `git show HEAD:X/...` 会作用在错误仓库上（路径不存在时返回该仓库 HEAD 的提交信息而非报错，是假阳性来源）；跑仓库命令前先确认 cwd。

## 2. 图片重命名（单个 commit）

从 `git diff --cached --name-status -z -M --diff-filter=R` 取 rename 对，按目标文件扩展名筛出图片（`.jpg/.png/...`），重建索引提交。注意可能有少量 xhtml rename 混在里面，要排除。

## 3. 图片引用更新（单个 commit）

从上一步的 rename 映射生成 `旧文件名→新文件名` 的 basename 映射（文件名含书号前缀，全局唯一；按 key 长度降序替换防子串歧义）。对每个修改的文本文件：`HEAD 内容 + 仅应用文件名替换` → 写入索引。**校验**：暂存 diff 的每个删除行都含旧图片名、新增行不含任何旧名。

## 4. 纯格式化变更（单个 commit）

对 HEAD 与新版做 HTML 解析对比，全部满足才算纯格式化：

- 文本内容（空白归一化）完全一致；
- 全部标签属性多重集合一致（任何 class/style/id/href 变化都排除）；
- 事件序列（文本块、img src、a href、`br`、其他标签）一致，唯一允许的增减是「无属性 `<p>` 紧跟 `<img>`」（图片被 p 包裹）。

DOCTYPE 添加、`</body>\n</html>` 合并、标签间换行等不产生任何事件差异，自然通过。`<br/>` 增减、插图位置移动、表格 `border` 属性等**会影响呈现，不算纯格式化**，留给下一阶段或人工。

## 5. 版式调整（单个 commit）

混合文件（版式 + 文本改动）的拆分：解析两侧文本块列表，difflib 对齐（文本块只受真实文字改动影响，img 移动/`br`/属性变化不影响对齐），把每个有差异的文本块**精确还原回 HEAD 原文**——在文件内容中定位该块的原始形态（注意实体编码：`&` 可能是 `&amp;`，要求唯一匹配），替换为旧块。最终验证「还原版与 HEAD 文本块完全一致」后写入索引。

- 文本块增删（非 1:1 replace）或定位歧义 → 无法安全分离，整文件留人工。
- 不要用 x2y.py 的 fixes 词典做全文件逆向替换来还原文本：常见词（如「其他」）会误伤天然出现的文本。词典也无法覆盖上游的人工修订。
- 同理，不要用「fixes 词典能否解释」来判定文本改动归属——上游修订很多不在词典里。

## 6. 文本改动按块分流

按相对路径配对 `X/<rel>` 与 `Y/<rel>`（两侧目录结构相同），各自提取相对 HEAD 的文本改动（文本块级 difflib，仅接受干净的 1:1 replace），然后**以改动块为最小单位**分类：

- 先把**旧 X 与旧 Y 的文本块序列做位置对齐**（1:1，允许旧 Y 被规则改写），把两侧改动配成「同位置块对」；对齐失败 → 复杂（`--commit` 中止，须人工处理）。规则的替换文本可能注入/消除带文本的标记（如 `<ruby><rt>` 注音），使 Y 侧块数与 X 不等：此类增删块在 HEAD 与工作中等量存在、不影响对齐，**HEAD 上 fixed(X) == Y 成立时容忍**（证明增删是规则产物），否则才算真复杂。必须按位置配对而不能只做全局片段集比较：同一最小片段（如 `冷气→空调`）可能一处是两侧共享改动、另一处是上游采纳规则，全局比较会把后者误判为正常同步。
- **结构改动文件**（单侧出现块增删或 N→M 替换，如上游插入译者注/特典条目、拆分合并段落）走块组级配对：以「映射后旧块锚点 + 操作类型 + 组长度」配对两侧 difflib 组，配对组做内容收敛验证（X 组文本应用规则后 == Y 组文本），1:1 组逐块走下方块级分类。全部块对收敛（sync/rulekilled）时批发成对提交；任一疑似块 → `--commit` 中止，先写规则。
- 逐块对比较**最小差异片段**集 F(x) 与 F(y)：F(x) == F(y) → 正常同步候选对；F(x) ⊃ F(y) 且 X 多出的片段能被 x2y.py 规则解释（收敛判定：反复应用「能严格缩小块差异」的规则后，剩余差异等于 F(y)）→ **规则收敛块对**：X 多出的片段是规则效果，随对批发提交；仅 X 有改动且收敛 → 规则采纳；Y 侧多出片段时，若 fixed 能分别从新旧 X 文本得到新旧 Y 文本（规则因上游编辑获得触发语境，如编辑引入「所以」触发 `由于→因为`），两侧差异纯属规则渲染 → 正常同步；其余（仅 Y 有改动、不收敛、HEAD 上该块两侧已有规则解释不了的不对称）→ 疑似上游错误（`--commit` 中止，须先写规则）。仅 X 有改动时除片段收敛外还认「规则管道等价」（apply_rules 后新旧 X 文本一致，上游改动整个落在规则覆盖内、fixed 后 Y 不变，覆盖 `….`→`……` 这类多步推导）为规则采纳。片段级比较是必要的：旧 Y 的同一句子可能已被 x2y 规则改过（如 `其它→其他`），块字面不同不代表改动不同。

| 块分类 | 处理 |
|---|---|
| 正常同步候选（`sync`） | 逐条人工/AI 审查改动内容，正常 → 批发提交 |
| 规则收敛（`mixed`）/ 规则采纳（`adopted`）/ 规则失效型收敛（`rulekilled`） | 两侧差异纯属规则渲染，随对批发提交 |
| 疑似上游错误（`suspect`）/ 复杂 | `--commit` 中止：先写规则（见下） |

**提交粒度为文件（批发提交）**：X 是上游逐字镜像（含已鉴定并写规则的缺陷文本），Y 是 `x2y(X)` 净化产物；`check_y_freshness`（run_all.py `--finish` 前置）保证不变式逐文件成立，因此收敛文件整文件成对提交。每文件一个 commit（X/Y 成对），提交信息 `fix: sync <相对路径>（N 处文本修订）` 的 N 计 sync+mixed 块。存在 suspect/复杂块或未覆盖改动（二进制、rename 等）时 `--commit` 中止并列出，全部处理完后重跑——**分流结束的必要条件之一是工作区清零**。

### 上游错误的规则化

可疑块写成 x2y 规则（分卷 TSV），重跑 `x2y.py` 后 Y 即净化、块转为规则渲染差异随批发提交：

- **校正规则**（缺陷文本 → 修正文本）：确认是上游错误且能给出修正时使用；有原文对照的规则在 TSV 中以规则上方的 `#` 注释附原文引文与理由（x2y 跳过 `#` 行）。
- **回钉规则**（新文本 → 旧文本）：存疑或无法给出修正时使用，把该句钉在旧译文上，Y 不含未确认文本。上游日后再改这句时规则失配失活（report_inactive_rules 报告），该句以新形态重新进入审查。
- 规则键要求：
  - 优先纯文本键：triage_text 的收敛判定在纯文本块上进行，含标签的键对收敛不可见，对应块会留在 suspect 里使 `--commit` 中止（此时人工确认 `check_y_freshness` 通过后按对人工提交，message 注明）。
  - 键在全卷 X 中的命中数必须恰为预期（防误伤，短键务必实测计数）。
  - 回钉/插入类规则的键不得命中旧文本（否则在旧文本上重复插入）；把键加长到只匹配新文本。
  - 默认写分卷规则限定作用域；跨卷通用的高置信缺陷才进 `_common.tsv`。

### 正常同步候选的正常性判断

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

难以判断的一律保留，宁漏勿错。

### 大规模 AI 审查（子代理流程）

候选块多、语义改动占比大时（如整卷重译波），派子代理分片对照日文原文审查：

```bash
uv run python scripts/sync/prepare_review.py   # 解析材料 → jp_text/ 原文纯文本 + review_chunks/ 任务块 + review_prompts/ 提示词
# delegate_task 每个 task_NN.md 派一个子代理（建议并发 ≤6，中断可安全重派）
uv run python scripts/sync/aggregate_verdicts.py  # 校验完整性 + 导出 suspect/unsure/unlocated
```

- 判定词汇（ok/suspect/unsure/unlocated）与原文检索纪律的权威版本在 prepare_review.py 的提示词模板内。index-jp 缺原文的卷 prepare 会列出，报用户补充后再核实。
- 子代理 verdict 是自报结论：suspect/unsure/unlocated 逐条人工复核、ok 抽查后，再放行或写规则。
- review_blocks.json 的块 id 是 verdict 的关联键。verdicts/ 非空时 prepare_review 拒绝重建（防在途任务被换底）；材料更新后需重审时，先聚合存档本轮 verdict，清空 verdicts/ 再重建。

### 配套机械项

- opf 的 `dcterms:modified` 时间戳更新：直接视为正常同步。
- 新增章节文件（X/Y 同时出现）：验证 Y 与 X 的文本差异能被 x2y.py 规则（含正词条目）解释 → 正常同步。
- 两侧同步的 xhtml rename：正常同步，由 `commit_xhtml_renames.py` 自动处理——所有文本 rename 一律提交「只移动不改内容」的纯移动（HEAD blob 写新路径，全部 R100 程序化验证；git 按相似度配对可能跨侧错配，脚本按文件名确定性重配，歧义挂起人工），捆绑改动随文件成为 M 态后**各回各的管道**（与非 rename 改动同语义）：标点/空白级 → 随 triage_text 正常分流；属性改动 → 版式批自动吸收；结构改动 → 结构文件通道（收敛批发/不收敛门控中止）；**语义片段 → 写入挂起清单 `.triage/hold.txt`**（M 态挂起，--commit 跳过且不计入门控），核实后删行随 `--finish` 批发提交。纯移动不放行任何未核实内容（新路径 = HEAD 字节）。
- 仅 X 侧的二进制/增删/rename：X 是上游镜像，按性质单独成 commit（`--commit` 会将其列为未覆盖改动并中止，人工提交后重跑即可）。两侧同步的二进制内容替换（如插图更新）：核对两侧逐字节一致后按 `fix: sync <路径>` 单独成 commit。

## 7. 规则失活验证（commit_adopted_x → report_inactive_rules，可重跑）

规则采纳/收敛块随 triage_text 批发提交；commit_adopted_x 在分类时导出 report_inactive_rules 候选（累积制 `adopted_rules.json`：与既有候选合并，收敛用到的规则 ∪ 命中改动块旧文本的规则 ∪ 规则失效块命中的规则，覆盖「上游采纳」与「上游改写源文本为第三种形式」两种失效路径，report_inactive_rules 消费后剔除）。两者均幂等可重跑。

- **commit_adopted_x**：导出上述候选；X 侧仍有规则采纳/失效收敛改动未收编时按块提交（规则采纳块提交 X 侧 `fix: sync X/<rel>（N 处文本修订，Y 侧规则渲染）`，规则失效型收敛块成对提交）。支持 `--dry-run`。
- **report_inactive_rules**：逐条机械验证候选规则是否失活——从管道移除该条后 fixed() 在
  **HEAD 与工作区**的 X 上输出均不变 → 当前失活，报告；否则报告首个
  反例（规则仍在他处生效）。失活规则保留在 rules/ 中不删除：X 持续增长，
  错误类规则可能在新内容复发，删除会把未来的漏校正变成人工审查成本，
  而保留的扫描开销可忽略（每条规则全语料约十几毫秒）。验证 HEAD X
  强制了顺序：相关改动未先提交时旧 X 仍命中规则，验证自动失败（报「仍生效」）。
  已验证失活与已不在 rules/ 中的候选从 `adopted_rules.json` 剔除，
  仍生效的保留待下轮。

## 8. 每批提交后的收尾

triage_text `--commit` 先批发提交全部文本对（**跳过挂起清单 `.triage/hold.txt` 中的 rel**），末尾再 `git add -A` 并核验工作区：清单外有未覆盖改动（二进制/rename/仅单侧增删等）即列出并以非零码中止——**文本对提交先于核验，列出项不影响已提交部分**，人工按性质提交后重跑 `--finish`；仅剩清单内改动时单列「挂起」报告并以非零码退出（轮次保持开放，下一轮带 zip 入仓仍被 update_x 拒绝）。核实完成后从清单删行重跑 `--finish`，直至输出「工作区已清零」。

挂起清单每行 `相对路径（不带 X/、Y/ 前缀）<TAB>理由`，`#` 开头为注释；失效条目（对应文件已无在途改动）每轮自动剔除。`rename:` 前缀条目由 commit_xhtml_renames 生成，其余为人工条目。清单内 rel 同时不计入提交门（suspect/complex）——held = 不提交也不阻塞。
