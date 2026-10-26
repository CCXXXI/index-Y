"""同步上游 X：index-X submodule fetch 后 checkout 到上游 ref，重跑 x2y。

上游每天对 EPUB/ 改动 force-push 日期 tag（YYYY.MM.DD）并发布 release；
默认对齐 origin/master 最新 commit，--ref 可指定其他 ref；历史 sha 仅支持 pin 的后代。
preflight（ensure_x）：submodule 未初始化则 init，override stub 缺失则补建。
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


def main() -> None:
    parser = triage_parser(__doc__)
    parser.add_argument(
        "--ref",
        default=None,
        help="上游 ref（tag/分支/sha）；默认 origin/master 最新 commit",
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
    ref = args.ref or "origin/master"
    sha = gitx("rev-parse", ref).decode().strip()
    gitx("checkout", "-q", sha)
    assert_x_clean(sha)  # checkout 后、渲染前再次绑定确切 HEAD 与干净源树
    tags = gitx("tag", "--points-at", "HEAD").decode().split()
    set_sync_ref(max(tags) if tags else sha[:10])
    print(f"index-X: {pin_sha()[:10]} -> {sha[:10]}（{ref}）", flush=True)
    x2y()


if __name__ == "__main__":
    main()
