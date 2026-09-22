# AGENTS.md

## 项目结构

- `X/`：上游 [index-X](https://github.com/1204244136/index-X)（粉丝校对版）的快照，上游持续更新。
- `rules/`：x2y 规则数据（严格 TSV：首行表头 `旧<TAB>新<TAB>注释`，规则一行一条 `旧<TAB>新<TAB>注释`，注释可空；`#` 开头的行是非规则行（禁用规则、卷级说明），同样 3 字段；空行禁止）。`_common.tsv` 为全卷通用段，`<卷名>.tsv` 为分卷段（文件名 = X/ 下卷目录名，无规则的卷不建文件）。
- `Y/`：对 X 运行 `scripts/sync/x2y.py` 的产物。Y 的一切内容都由 X 派生，不直接编辑。
- `scripts/sync/`：同步并分流——`x2y.py`（X → Y 转换引擎，加载 `rules/` 并应用：分卷规则先于通用规则，文件内自上而下）、`update_x.py`（上游 zip 入仓）、`run_all.py` 驱动的分流流水线 + 共用库。
- `scripts/active_rules.py`：报告范围内当前生效的 x2y 规则及首次生效点，供反馈上游。
- `scripts/proofreading_locate.py`：阅读校对定位器（关键词清单 → X/ 全语料计数、目标卷 X/Y 上下文、日文原文侧命中）。
- `scripts/agent_proofread/`：agent 通读校对——`plan_chunks.py`（整卷正文切块方案）、`verify_findings.py`（子 agent 发现复核：引文/日文依据逐字验证 + X/Y 比对）、`apply_rules.py`（候选规则落分卷 TSV：归属计数、正则守卫、双模拟）。
- `scripts/tag.py`：发新版本（打 tag 触发 release 编译 epub）。
- `.agents/skills/`：任务流程 project skills（`hermes skills trust <仓库路径>` 后按需加载）。
- `../index-jp`（同级目录，如存在）：私有日文原文仓库，校对审查时读取对照，其结构与约定见该仓库 AGENTS.md（读取该目录时自动注入）。可在 `rules/` 注释中摘抄原文片段作为规则依据，但不得将其内容批量导入本仓库。
- `../index-X`（同级目录，如存在）：上游仓库的完整本地 clone，只读参考（commit 历史、`docs/maintenance-records/` 改动理由记录、`.agents/` 术语政策）；同步分流时由 `scripts/sync/upstream_context.py` 经 git plumbing 读其 origin/master，不依赖其工作区状态。

不变式：**X 是上游逐字镜像，Y 是 `x2y(X)` 净化产物。**

## 脚本开发

- 功能优先调用成熟库实现（如 argparse 解析命令行、colorama 输出颜色），不手写等价逻辑（如自行解析 sys.argv、拼 ANSI 转义序列）。
- 新增或修改 `scripts/` 后，提交前必须跑通 `uv run ruff check .` 与 `uv run ty check .`（零报告才算通过）。

## 任务流程

- **同步上游**（上游 zip 更新入仓并分流提交）：流程见 `.agents/skills/sync-triage/SKILL.md`。入口：`uv run python scripts/sync/run_all.py <下载的 zip>`。
- **阅读校对**（维护者阅读 Y 产物时记下的可疑词句）：流程见 `.agents/skills/proofreading/SKILL.md`。
- **agent 通读校对**（子 agent 按章通读整卷 Y 产物找问题）：流程见 `.agents/skills/agent-proofread/SKILL.md`。

## 提交信息

遵循 Conventional Commits；body 附最小差异摘要（`旧→新`），便于日后回溯。各任务的提交命名见对应 skill。
