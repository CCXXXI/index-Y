"""批次 1：图片重命名 → 单个 commit。

把暂存的 rename 中的图片项单独提交；rename 映射写入共享状态目录供批次 2 使用。
用法: uv run python scripts/sync/s1_commit_image_renames.py [--dry-run]
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import IMG_EXT, git, staged_renames

STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", "/tmp"), "index-Y-triage")


def main() -> None:
    dry = "--dry-run" in sys.argv
    git("add", "-A")  # 统一从「全部暂存」状态出发
    pairs = staged_renames()
    img = [(f, t) for f, t in pairs if t.lower().endswith(IMG_EXT)]
    other = [(f, t) for f, t in pairs if not t.lower().endswith(IMG_EXT)]
    print(f"rename 总数 {len(pairs)}，图片 {len(img)}，非图片（跳过） {len(other)}")
    for f, t in other:
        print(f"  跳过: {f} -> {t}")

    # basename -> basename（含书号前缀，全局唯一）；无图片重命名时也写空映射，
    # 供批次 2 判断「本批无引用要更新」
    os.makedirs(STATE_DIR, exist_ok=True)
    m = {os.path.basename(f): os.path.basename(t) for f, t in img}
    # 与已有映射合并（X/Y 两树的 rename 可能分属不同批次）
    map_path = os.path.join(STATE_DIR, "rename_map.json")
    if os.path.exists(map_path):
        with open(map_path, encoding="utf-8") as f:
            m = {**json.load(f), **m}
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)

    if not img:
        print("无图片重命名")
        return
    if dry:
        return

    # 重建索引：只暂存图片 rename（from 删除 + to 新增）
    git("reset", "-q")
    git("add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
        input_bytes="\0".join(p for pr in img for p in pr).encode("utf-8"))
    n_staged = len(staged_renames())
    assert n_staged == len(img), f"暂存 rename 数 {n_staged} != {len(img)}"

    git("commit", "-q", "-m", "refactor: rename image files")
    git("add", "-A")
    print(f"已提交 {len(img)} 个图片重命名；映射共 {len(m)} 条 -> {map_path}")


if __name__ == "__main__":
    main()
