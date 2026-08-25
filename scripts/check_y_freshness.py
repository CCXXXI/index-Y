"""前置校验：确认 Y 树与当前 X 同步（Y == x2y(X)）。

分流前必须跑。Y 滞后会让「两侧不一致」「仅 X 改动」桶系统性失真。
用法: uv run python scripts/check_y_freshness.py
退出码 0 = 新鲜；1 = 存在滞后文件（需 uv run python x2y.py 后重新分流）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib_triage import TEXT_EXT, repo_root  # noqa: E402
from x2y import fixes  # noqa: E402  复用规则表，fixed() 的逻辑在下面对 bytes 重写

import regex as re  # noqa: E402  规则含 \p{}，stdlib re 不支持


def fixed(vol: str, content: str) -> str:
    for old, new in fixes.get(vol, []) + fixes["*"]:
        content = re.sub(old, new, content)
    return content


def main() -> int:
    root = repo_root()
    stale = []
    for vol in sorted(os.listdir(os.path.join(root, "X"))):
        vol_dir = os.path.join(root, "X", vol)
        if not os.path.isdir(vol_dir):
            continue
        for dirpath, _, files in os.walk(vol_dir):
            for name in files:
                if not name.lower().endswith(TEXT_EXT):
                    continue
                xp = os.path.join(dirpath, name)
                rel = os.path.relpath(xp, os.path.join(root, "X"))
                yp = os.path.join(root, "Y", rel)
                if not os.path.exists(yp):
                    stale.append((rel, "Y 缺失"))
                    continue
                with open(xp, encoding="utf-8") as f:
                    expect = fixed(vol, f.read())
                with open(yp, encoding="utf-8") as f:
                    actual = f.read()
                if expect != actual:
                    stale.append((rel, "内容不一致"))
    if stale:
        print(f"Y 滞后: {len(stale)} 个文件与 x2y(X) 不一致")
        for rel, why in stale[:20]:
            print(f"  {why}: {rel}")
        if len(stale) > 20:
            print(f"  ... 共 {len(stale)} 个")
        print("请先运行: uv run python x2y.py")
        return 1
    print("Y 与当前 X 同步 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
