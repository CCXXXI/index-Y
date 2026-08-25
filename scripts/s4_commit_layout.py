"""批次 4：版式调整 → 单个 commit。

对每个混合文件生成「新结构 + 旧文本」的中间版本（revert_text_chunks），
验证与 HEAD 文本块完全一致后入提交。纯文本改动的文件不受影响（留审）。
用法: uv run python scripts/s4_commit_layout.py [--dry-run]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import (TEXT_EXT, CatFile, git, head_sha_map, norm_ws,
                        revert_text_chunks, stage_content, text_chunks)  # noqa: E402


def main() -> None:
    dry = "--dry-run" in sys.argv
    git("add", "-A")
    entries = git("status", "--porcelain", "-z").decode("utf-8").split("\0")
    cands = [e[3:] for e in entries
             if e and e[0] == "M" and not e.startswith("R")
             and e[3:].lower().endswith(TEXT_EXT)]
    print(f"候选 M 文本文件 {len(cands)}")

    hm = head_sha_map()
    cf = CatFile()
    to_stage, text_only, left = [], [], []
    for path in cands:
        old_t = cf.read(hm[path]).decode("utf-8")
        new_t = open(os.path.join(os.getcwd(), path), encoding="utf-8").read()
        try:
            staged = revert_text_chunks(old_t, new_t)
        except Exception:
            staged = None
        if staged is None:
            left.append(path)
        elif staged == old_t:
            text_only.append(path)
        else:
            to_stage.append((path, staged))
    cf.close()
    print(f"版式入提交 {len(to_stage)}，纯文本留审 {len(text_only)}，无法分离留审 {len(left)}")
    if dry or not to_stage:
        return

    git("reset", "-q")
    for path, content in to_stage:
        stage_content(path, content.encode("utf-8"))

    # 校验：暂存区每个文件与 HEAD 文本块完全一致
    idx = {}
    for ent in git("ls-files", "-s", "-z").decode("utf-8").split("\0"):
        if not ent:
            continue
        meta, path = ent.split("\t", 1)
        idx[path] = meta.split()[1]
    cf = CatFile()
    bad = 0
    for path, _ in to_stage:
        old_chunks = [norm_ws(c) for c in text_chunks(cf.read(hm[path]))]
        new_chunks = [norm_ws(c) for c in text_chunks(cf.read(idx[path]))]
        if old_chunks != new_chunks:
            bad += 1
            print("  文本不一致:", path)
    cf.close()
    assert bad == 0, f"{bad} 个文件校验失败，中止"
    git("commit", "-q", "-m",
        "refactor: layout adjustments (structure/attributes only, no text changes)")
    git("add", "-A")
    print("已提交版式调整")


if __name__ == "__main__":
    main()
