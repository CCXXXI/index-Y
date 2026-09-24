---
name: proofreading
description: "Use when 阅读校对：核实维护者阅读 Y 产物时记下的可疑词句。"
---

# 阅读校对

维护者阅读 Y 产物时记下的可疑词句不经分流流程，直接核实修正：

1. 在 `index-X/EPUB` 与 `EPUB` 同时搜索，确认词句仍存在于当前版本。
2. 逐条对照原文判定（对照方法见 index-jp AGENTS.md）：确认错误写校正规则；核对无误则不改并回复依据——可疑不等于错误。
3. 规则归属：旧串 `rg -F` 在 `index-X/EPUB` 全语料计数，唯一命中入分卷段，多卷复现入 `_common.tsv`。旧串须逐字匹配原始 xhtml（ruby 注音以标签内联，如 `一方通行<rt>Accelerator</rt>`），长串截断在 ruby 边界外。原文引文与理由写入规则的注释列（第三列）。
4. 新串写终态：分卷规则先跑，产物还会被 `_common.tsv` 的通用规则改写，注意叠加后的最终形态。
5. 重跑 `scripts/sync/x2y.py`，核验 Y 侧改动与判定逐条对应，rules/ 与 EPUB/ 改动同一 commit 提交。

定位与计数用 `uv run python scripts/proofreading_locate.py <卷> 关键词清单.txt`（清单 UTF-8 一行一条，TAB 后可附日文原文关键词）：一次输出 X 侧全语料计数（规则归属判定）、目标卷 X/Y 逐字上下文（供旧串截取）、原文侧命中（已剥 `<rt>` 归并空白）。

## 经验

- 新串与 `_common` 正则（lookaround、删除型规则等）是否相互作用一律实测，不读正则脑补：`import` `scripts/sync/x2y.py` 的 `fixed(卷名, 候选串)` 可直接跑完整管道验证终态。

提交命名 `fix: 校正 <卷标识>（N 处文本修订）`；body 附差异摘要（`旧→新`）与原文依据。
