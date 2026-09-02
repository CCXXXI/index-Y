"""一键跑完自动分流流程，代替按文档逐条执行 s1~s6b。

默认（人工审查前）：前置校验 → s1~s4 → s5 分类导出审查材料 → s6a → s6b。
带 zip 时先跑 update_x.py（上游入仓）再接默认流程——要求工作区干净，
上轮未收尾会被拒绝（在途审查会被新版顶掉）；在途分流中重跑不带 zip。
--finish（人工审查后）：s5 --commit → s6a → s6b 收尾。

任一脚本失败即中止。各步骤本身幂等，可整体重跑。
用法: uv run python scripts/sync/s0_run_all.py [上游.zip] [--finish]
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import triage_parser

SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def run(script: str, *args: str) -> None:
    print(f"\n===== {script} {' '.join(args)} =====", flush=True)
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                       check=False)
    if r.returncode != 0:
        raise SystemExit(f"{script} 失败（exit {r.returncode}），中止")


def update_x(zip_path: Path) -> None:
    """上游 zip 入仓前置：工作区必须干净，否则在途审查会被新版顶掉。

    跨轮残留本身是安全的（docs/sync-triage.md），此 gate 防的是白费在途
    审查与一轮混入两个上游 delta；确认要强行并入可手动跑 update_x.py。
    """
    r = subprocess.run(["git", "status", "--porcelain", "-z"],
                       capture_output=True, text=True, check=True)
    if r.stdout:
        raise SystemExit(
            "错误：工作区有未提交改动，拒绝并入新上游（上轮未收尾，在途审查"
            "会被新版顶掉）。先 --finish 收尾；确认放弃在途审查则手动运行 "
            "uv run python scripts/sync/update_x.py <zip>")
    if not zip_path.is_file():
        raise SystemExit(f"错误：zip 不存在：{zip_path}")
    run("update_x.py", str(zip_path))


def main() -> None:
    parser = triage_parser(__doc__)
    parser.add_argument("zip", nargs="?", type=Path,
                        help="上游下载的 zip；提供时先运行 update_x 起新轮")
    parser.add_argument("--finish", action="store_true",
                        help="人工审查后收尾：s5 --commit → s6a → s6b")
    args = parser.parse_args()
    if args.finish:
        if args.zip is not None:
            parser.error("--finish 不接受 zip 参数")
        run("check_y_freshness.py")  # 批发提交的不变式前提：Y == x2y(X)
        run("s5_triage_text.py", "--commit")
        run("s6a_commit_adopted_x.py")
        run("s6b_report_inactive_rules.py")
        return
    if args.zip is not None:
        update_x(args.zip)
    run("check_y_freshness.py")
    run("s1_commit_image_renames.py")
    run("s2_commit_image_refs.py")
    run("s3_commit_pure_formatting.py")
    run("s4_commit_layout.py")
    run("s5_triage_text.py")
    run("s6a_commit_adopted_x.py")
    run("s6b_report_inactive_rules.py")


if __name__ == "__main__":
    main()
