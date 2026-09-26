"""一键跑完自动分流流程，代替按文档逐条执行各步脚本。

默认（人工审查前）：check_y_freshness 前置校验 → triage_text 导出审查
材料（Y 侧文本块 diff）→ upstream_context（pin 区间记录 + ★ 预注）。
--sync 时先跑 update_x.py（index-X submodule checkout 上游 ref 起新轮）再接
默认流程——要求工作区干净且 submodule HEAD == pin，上轮未收尾会被拒绝
（在途审查会被新版顶掉）；另校验 HEAD 自洽（Y == x2y(X)），拦上轮改 rules/
后漏跑 x2y.py 的陈旧 Y；在途分流中重跑不带 --sync。
--finish（人工审查后）：check_y_freshness → triage_text --commit（原子同步
提交，纯上游形态）；rules/ 有在途改动时自动 stash 规则 → 原子提交 → 恢复 →
重渲染，报告待提交清单并以非零码退出（轮次保持开放）——规则提交
（rules/ 与 Y 侧规则效果同 commit，diff 即规则生效形态）落地后本轮才收尾。

任一脚本失败即中止。各步骤本身幂等，可整体重跑。
用法: uv run python scripts/sync/run_all.py [--sync [REF]] [--finish]
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_triage import ensure_x, pin_sha, triage_parser, x_head

SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def run(script: str, *args: str) -> None:
    print(f"\n===== {script} {' '.join(args)} =====", flush=True)
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, script), *args], check=False
    )
    if r.returncode != 0:
        raise SystemExit(f"{script} 失败（exit {r.returncode}），中止")


def git_ok(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, env=env
    )


def finish() -> None:
    run("check_y_freshness.py")  # 原子提交的不变式前提：Y == x2y(X)
    if not git_ok("status", "--porcelain", "-z", "--", "rules/").stdout:
        run("triage_text.py", "--commit")
        return
    # rules/ 有在途改动：先 stash 规则落纯上游形态的原子同步提交，再恢复规则
    # 并重渲染——随后的规则提交（rules/ 与 Y 侧规则效果同 commit）diff 即生效形态
    r = git_ok("stash", "push", "-u", "-m", "run_all --finish 在途规则", "--", "rules/")
    if r.returncode != 0:
        raise SystemExit(f"git stash push rules/ 失败：\n{r.stdout}{r.stderr}")
    try:
        run("x2y.py")  # HEAD 规则重渲染：Y 回到纯上游形态
        run("triage_text.py", "--commit")
    finally:
        r = git_ok("stash", "pop")
        if r.returncode != 0:
            raise SystemExit(
                "git stash pop 失败：在途规则改动仍保留在 stash 中"
                "（git stash list 可见，恢复后重跑 x2y.py）：\n"
                f"{r.stdout}{r.stderr}"
            )
    run("x2y.py")  # 恢复在途规则后重渲染：Y 侧相对 HEAD 只剩规则效果
    left = [e for e in git_ok("status", "--porcelain", "-z").stdout.split("\0") if e]
    listing = "\n".join(f"  {e[:2]} {e[3:]}" for e in left)
    raise SystemExit(
        "原子同步提交已完成（纯上游形态）。以下在途改动应作为单个规则提交入仓"
        "（rules/ 与 Y 侧规则效果同 commit，diff 即规则生效形态；body 附 旧→新 "
        "最小差异摘要），提交后工作区清零，本轮收尾完成（非失败，无需再跑 --finish）：\n"
        + listing
    )


def update_x(ref: str | None) -> None:
    """起新轮前置：工作区必须干净、submodule 已在 pin 上，否则在途审查会被新版顶掉。

    跨轮残留本身是安全的（.agents/skills/sync-triage/SKILL.md），此 gate 防的是白费在途
    审查与一轮混入两个上游 delta；确认要强行并入可手动跑 update_x.py。
    """
    r = subprocess.run(
        ["git", "status", "--porcelain", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    if r.stdout:
        raise SystemExit(
            "错误：工作区有未提交改动，拒绝并入新上游（上轮未收尾，在途审查"
            "会被新版顶掉）。先 --finish 收尾；确认放弃在途审查则手动运行 "
            "uv run python scripts/sync/update_x.py"
        )
    if x_head() != pin_sha():
        raise SystemExit(
            "错误：submodule HEAD 已离开 pin（上轮在途），拒绝再并入。先 --finish "
            "收尾；确认放弃在途审查则手动运行 uv run python scripts/sync/update_x.py"
        )
    # HEAD 自洽门控：工作区干净时等价于校验 HEAD——上轮改 rules/ 后漏跑
    # x2y.py 会留下陈旧 Y，本轮重跑 x2y 会把规则补渲染混进审查
    print("\n===== check_y_freshness.py（HEAD 自洽门控） =====", flush=True)
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "check_y_freshness.py")], check=False
    )
    if r.returncode != 0:
        raise SystemExit(
            "HEAD 的 X/Y/rules 不自洽（上轮改 rules/ 后漏跑 x2y.py？）。先重跑 "
            "uv run python scripts/sync/x2y.py，把 Y 侧规则效果提交，再开始新一轮"
        )
    run("update_x.py", *(["--ref", ref] if ref else []))


def main() -> None:
    parser = triage_parser(__doc__)
    parser.add_argument(
        "--sync",
        nargs="?",
        const="",
        metavar="REF",
        help="起新轮：先运行 update_x（index-X checkout 上游 ref；缺省最新 release tag）",
    )
    parser.add_argument(
        "--finish",
        action="store_true",
        help="人工审查后收尾：原子同步提交（纯上游形态）；rules/ 在途改动自动延迟为随后的规则提交",
    )
    args = parser.parse_args()
    ensure_x()
    if args.finish:
        if args.sync is not None:
            parser.error("--finish 不接受 --sync 参数")
        finish()
        return
    if args.sync is not None:
        update_x(args.sync or None)
    run("check_y_freshness.py")
    run("triage_text.py")
    # 上游上下文（commit/记录预注）须在 triage_text 之后：预注基于其刚覆写
    # 的审查材料；起新轮时带 --fetch，在途重跑离线复用
    run("upstream_context.py", *(["--fetch"] if args.sync is not None else []))


if __name__ == "__main__":
    main()
