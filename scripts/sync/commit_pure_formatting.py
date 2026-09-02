"""纯格式化变更 → 单个 commit。

判定（HTML 解析对比 HEAD 与新版，全部满足才算）：
- 文本内容（空白归一化）完全一致
- 标签属性多重集合一致
- 事件序列一致，仅允许「无属性 <p> 包裹 <img>」的增减
用法: uv run python scripts/sync/commit_pure_formatting.py [--dry-run]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import (
    CatFile,
    git,
    head_sha_map,
    is_pure_formatting,
    modified_text_files,
    triage_parser,
)


def main() -> None:
    parser = triage_parser(__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计，不实际提交")
    dry = parser.parse_args().dry_run
    cands = modified_text_files()
    print(f"候选 M 文本文件 {len(cands)}")

    hm = head_sha_map()
    cf = CatFile()
    passed = []
    for path in cands:
        try:
            with open(os.path.join(os.getcwd(), path), "rb") as f:
                cur = f.read()
            if is_pure_formatting(cf.read(hm[path]), cur):
                passed.append(path)
        except Exception:  # noqa: BLE001, S110
            pass  # 解析失败一律留审
    cf.close()
    print(f"纯格式化 {len(passed)}")
    if dry or not passed:
        return

    git("reset", "-q")
    git("add", "--pathspec-from-file=-", "--pathspec-file-nul",
        input_bytes="\0".join(passed).encode("utf-8"))
    n = len([p for p in git("diff", "--cached", "--name-only", "-z")
             .decode("utf-8").split("\0") if p])
    assert n == len(passed), f"暂存 {n} != {len(passed)}"
    git("commit", "-q", "-m", "style: pure formatting changes (no text/display impact)")
    git("add", "-A")
    print("已提交纯格式化变更")


if __name__ == "__main__":
    main()
