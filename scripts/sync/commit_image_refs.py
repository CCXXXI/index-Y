"""图片引用更新 → 单个 commit。

读取 commit_image_renames 写出的 rename 映射，对每个修改的文本文件生成
「HEAD + 仅图片名替换」的中间版本写入索引。
校验：暂存 diff 的每个删除行都含旧图名、新增行不含旧名。
用法: uv run python scripts/sync/commit_image_refs.py [--dry-run]
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from commit_image_renames import STATE_DIR
from lib_triage import (
    CatFile,
    git,
    head_sha_map,
    modified_text_files,
    stage_content,
    triage_parser,
)


def main() -> None:
    parser = triage_parser(__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计，不实际提交")
    dry = parser.parse_args().dry_run
    map_path = os.path.join(STATE_DIR, "rename_map.json")
    if not os.path.exists(map_path):
        raise SystemExit(f"缺少 {map_path}，请先运行 commit_image_renames.py")
    with open(map_path, encoding="utf-8") as f:
        renames: dict[str, str] = json.load(f)
    print(f"映射 {len(renames)} 条")
    if not renames:
        return
    keys = sorted(renames, key=len, reverse=True)  # 长 key 优先，防子串歧义
    pattern = re.compile("|".join(re.escape(k) for k in keys))

    cands = modified_text_files()
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
        elif (line.startswith("+") and not line.startswith("+++")
              and any(k in line for k in renames)):
            bad += 1
    assert bad == 0, f"暂存区含 {bad} 行非图片路径变更，中止"
    git("commit", "-q", "-m", "refactor: update image references for renamed files")
    git("add", "-A")
    print("已提交图片引用更新")


if __name__ == "__main__":
    main()
