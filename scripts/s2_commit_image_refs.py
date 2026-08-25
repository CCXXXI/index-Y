"""批次 2：图片引用更新 → 单个 commit。

读取批次 1 写出的 rename 映射，对每个修改的文本文件生成「HEAD + 仅图片名替换」
的中间版本写入索引。校验：暂存 diff 的每个删除行都含旧图名、新增行不含旧名。
用法: uv run python scripts/s2_commit_image_refs.py [--dry-run]
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import (TEXT_EXT, CatFile, git, head_sha_map, stage_content)  # noqa: E402
from s1_commit_image_renames import STATE_DIR  # noqa: E402


def main() -> None:
    dry = "--dry-run" in sys.argv
    map_path = os.path.join(STATE_DIR, "rename_map.json")
    if not os.path.exists(map_path):
        raise SystemExit(f"缺少 {map_path}，请先运行 s1_commit_image_renames.py")
    renames = json.load(open(map_path, encoding="utf-8"))
    keys = sorted(renames, key=len, reverse=True)  # 长 key 优先，防子串歧义
    pattern = re.compile("|".join(re.escape(k) for k in keys))
    print(f"映射 {len(renames)} 条")

    git("add", "-A")
    entries = git("status", "--porcelain", "-z").decode("utf-8").split("\0")
    cands = [e[3:] for e in entries
             if e and e[0] == "M" and not e.startswith("R")
             and e[3:].lower().endswith(TEXT_EXT)]
    print(f"候选 M 文本文件 {len(cands)}")

    hm = head_sha_map()
    cf = CatFile()
    touched = []
    for path in cands:
        try:
            text = cf.read(hm[path]).decode("utf-8")
        except (UnicodeDecodeError, KeyError):
            continue
        new_text = pattern.sub(lambda m: renames[m.group(0)], text)
        if new_text != text:
            touched.append((path, new_text))
    cf.close()
    print(f"含图片路径更新 {len(touched)}")
    if dry or not touched:
        return

    git("reset", "-q")
    for path, content in touched:
        stage_content(path, content.encode("utf-8"))

    # 校验：暂存 diff 全部变更行都是图片名替换
    bad = 0
    for line in git("diff", "--cached", "--unified=0").decode("utf-8", "replace").splitlines():
        if line.startswith("-") and not line.startswith("---"):
            if not any(k in line for k in renames):
                bad += 1
        elif line.startswith("+") and not line.startswith("+++"):
            if any(k in line for k in renames):
                bad += 1
    assert bad == 0, f"暂存区含 {bad} 行非图片路径变更，中止"
    git("commit", "-q", "-m", "refactor: update image references for renamed files")
    git("add", "-A")
    print("已提交图片引用更新")


if __name__ == "__main__":
    main()
