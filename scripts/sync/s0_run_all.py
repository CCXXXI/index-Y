"""一键跑完自动分流流程，代替按文档逐条执行 s1~s6b。

默认（人工审查前）：前置校验 → s1~s4 → s5 分类导出审查材料 → s6a → s6b。
--finish（人工审查后）：s5 --commit → s6a → s6b 收尾。

任一脚本失败即中止。各步骤本身幂等，可整体重跑。
用法: uv run python scripts/sync/s0_run_all.py [--finish]
"""

import os
import subprocess
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def run(script: str, *args: str) -> None:
    print(f"\n===== {script} {' '.join(args)} =====", flush=True)
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                       check=False)
    if r.returncode != 0:
        raise SystemExit(f"{script} 失败（exit {r.returncode}），中止")


def main() -> None:
    if "--finish" in sys.argv:
        run("check_y_freshness.py")  # 批发提交的不变式前提：Y == x2y(X)
        run("s5_triage_text.py", "--commit")
        run("s6a_commit_adopted_x.py")
        run("s6b_report_inactive_rules.py")
        return
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
