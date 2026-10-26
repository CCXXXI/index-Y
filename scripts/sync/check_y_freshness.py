"""前置校验：确认 Y 树与当前 X 同步（Y == x2y(X)）。

分流前必须跑。Y 滞后会让「两侧不一致」「仅 X 改动」桶系统性失真。
用法: uv run python scripts/sync/check_y_freshness.py
退出码 0 = 新鲜；1 = 存在滞后文件（需 uv run python scripts/sync/x2y.py 后重新分流）。
"""

import sys

from lib_triage import (
    assert_x_clean,
    ensure_x,
    freshness_errors,
    repo_root,
    triage_parser,
)
from x2y import fixed


def main() -> int:
    triage_parser(__doc__).parse_args()
    ensure_x()  # submodule init + override stub（幂等）
    assert_x_clean()  # freshness 的源必须是 HEAD，而非 tracked-dirty 工作区
    root = repo_root()
    stale = freshness_errors(root, fixed)
    if stale:
        print(f"Y 滞后: {len(stale)} 个文件与 x2y(X) 不一致")
        for rel, why in stale[:20]:
            print(f"  {why}: {rel}")
        if len(stale) > 20:
            print(f"  ... 共 {len(stale)} 个")
        print("请先运行: uv run python scripts/sync/x2y.py")
        return 1
    print("Y 与当前 X 同步 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
