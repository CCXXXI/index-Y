"""同步上游 X：index-X submodule fetch 后 checkout 到 release tag（或指定 ref），重跑 x2y。

上游每天对 EPUB/ 改动 force-push 日期 tag（YYYY.MM.DD）并发布 release；
默认对齐最新 tag（与发布制品语义同构），--ref 可指定其他 ref
（如 origin/master 追最尖、或历史 tag 复现）。
preflight（ensure_x）：submodule 未初始化则 init，X/ 联接与 override stub 缺失则补建。
门控：submodule 工作区必须干净；submodule HEAD ≠ pin 时警告（上轮同步在途，继续
将顶掉在途审查——确认强行并入才手动跑本脚本；run_all 已先行拒绝）。
用法: uv run python scripts/sync/update_x.py [--ref REF]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import (
    assert_x_clean,
    ensure_x,
    gitx,
    pin_sha,
    set_sync_ref,
    triage_parser,
    x_head,
)
from x2y import x2y


def latest_tag() -> str:
    """最新 release tag（YYYY.MM.DD 字典序 == 日期序）。"""
    tags = sorted(t for t in gitx("tag", "-l", "20*").decode().split() if t)
    if not tags:
        sys.exit("错误：index-X 中找不到日期 tag（20*）")
    return tags[-1]


def main() -> None:
    parser = triage_parser(__doc__)
    parser.add_argument(
        "--ref",
        default=None,
        help="上游 ref（tag/分支/sha）；默认最新 release tag",
    )
    args = parser.parse_args()
    ensure_x()
    assert_x_clean()
    if x_head() != pin_sha():
        print(
            "警告：submodule HEAD 已离开 pin（上轮同步在途），继续将顶掉在途审查",
            flush=True,
        )

    print("fetch index-X ...", flush=True)
    gitx("fetch", "-q", "--tags", "--force", "origin")
    ref = args.ref or latest_tag()
    sha = gitx("rev-parse", ref).decode().strip()
    gitx("checkout", "-q", sha)
    tags = gitx("tag", "--points-at", "HEAD").decode().split()
    set_sync_ref(max(tags) if tags else sha[:10])
    print(f"index-X: {pin_sha()[:10]} -> {sha[:10]}（{ref}）", flush=True)
    x2y()


if __name__ == "__main__":
    main()
