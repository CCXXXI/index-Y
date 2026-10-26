"""X/Y 双校重合度分析：本项目校对提交 vs 上游通校提交，逐处比对。

用途：上游（index-X）对某卷做完全卷通校后，评估本项目已落地规则与上游改动
的重合度，找出上游漏修（可反馈）与双方一致（互证）的发现。

用法：
    uv run python scripts/proofread_overlap.py S1_01 \
        --y-commit c2bf7be0 \
        --x-commits 57c1f162 610f96a7 68aba2b5 012d8a4f

输出：Y 侧每处修订一行——行级匹配（上游通校是否改了同一行）+ 跨度比对
（上游改动跨度是否含 Y 旧串）+ 当前 X 语料存亡确认。等价/重写/漏修的最终
裁决需人工读表（新旧跨度并列印出）。

口径说明：
- 改动单位 = diff 行对（x2y 规则为纯行内替换，上游通校亦按行统计规模）。
- 行级匹配：同文件内上游改动行与 Y 改动行归一化相似度 ≥0.9。
- 纯行级匹配会虚高（上游通校改动面约 3/4 行），故必须再看跨度包含。
"""

import argparse
import re
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
X_REPO = REPO_ROOT / "index-X"
Y_REPO = REPO_ROOT

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def norm(text: str) -> str:
    """剥标签、并空白，供跨仓库/跨规则层的文本比对。"""
    return WS_RE.sub("", TAG_RE.sub("", text))


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr[:500]}")
    return r.stdout


@dataclass
class Pair:
    file: str  #  basename，如 S1_01-02_Chapter1.xhtml
    old: str
    new: str


def extract_pairs(repo: Path, commit: str, vol_code: str) -> list[Pair]:
    """提取单个提交中该卷正文 xhtml 的改动行对（--unified=0，按序配对）。"""
    out = git(repo, "show", commit, "--format=", "--unified=0")
    pairs: list[Pair] = []
    cur: str | None = None
    minus: list[str] = []
    plus: list[str] = []

    def flush() -> None:
        assert cur is not None  # 调用点均以 cur 非空为条件
        for i in range(max(len(minus), len(plus))):
            o = minus[i] if i < len(minus) else ""
            n = plus[i] if i < len(plus) else ""
            pairs.append(Pair(cur, o, n))

    for line in out.splitlines():
        if line.startswith("+++ b/"):
            if cur and (minus or plus):
                flush()
            minus, plus = [], []
            path = line[6:].strip()  # 行尾可能带 \t
            base = path.rsplit("/", 1)[-1]
            if (
                base.startswith(vol_code)
                and "/Text/" in path
                and path.endswith(".xhtml")
            ):
                cur = base
            else:
                cur = None
        elif cur and line.startswith("-") and not line.startswith("---"):
            minus.append(line[1:])
        elif cur and line.startswith("+") and not line.startswith("+++"):
            plus.append(line[1:])
        elif line.startswith(("@@", "diff ")):
            if cur and (minus or plus):
                flush()
            minus, plus = [], []
    if cur and (minus or plus):
        flush()
    return pairs


def changed_spans(old: str, new: str) -> tuple[list[str], list[str]]:
    """old/new 行内变化跨度（raw），按 opcodes 顺序对齐。"""
    sm = SequenceMatcher(None, old, new)
    olds, news = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            olds.append(old[i1:i2])
            news.append(new[j1:j2])
    return olds, news


def current_corpus(vol_dir: str) -> str:
    """当前 X HEAD 该卷正文全量（归一化），用于确认旧串存亡。"""
    names = git(
        X_REPO, "ls-tree", "--name-only", "-r", "HEAD", f"{vol_dir}/OEBPS/Text/"
    )
    blob = ""
    for n in names.splitlines():
        if n.endswith(".xhtml"):
            blob += git(X_REPO, "show", f"HEAD:{n}")
    return norm(blob)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("vol", help="卷码（如 S1_01），须为正文文件 basename 前缀")
    ap.add_argument("--y-commit", required=True, help="本项目校对提交")
    ap.add_argument(
        "--x-commits", nargs="+", required=True, help="上游通校 campaign 提交"
    )
    args = ap.parse_args()

    y_pairs = extract_pairs(Y_REPO, args.y_commit, args.vol)
    x_pairs: list[Pair] = []
    for c in args.x_commits:
        x_pairs += extract_pairs(X_REPO, c, args.vol)

    # 当前 X 卷目录名（归一化查找，兼容改名）
    ls = git(X_REPO, "ls-tree", "--name-only", "HEAD", "EPUB/")
    vol_dir = next(
        (d for d in ls.splitlines() if d.startswith(f"EPUB/【{args.vol}】")),
        None,
    )
    cur = current_corpus(vol_dir) if vol_dir else ""

    x_by_file: dict[str, list[Pair]] = {}
    for p in x_pairs:
        x_by_file.setdefault(p.file, []).append(p)

    n_untouched = n_line_match = n_span = n_gone = 0
    for idx, y in enumerate(y_pairs):
        yo_n = norm(y.old)
        best, br = None, 0.0
        for x in x_by_file.get(y.file, []):
            r = SequenceMatcher(None, yo_n, norm(x.old)).ratio()
            if r > br:
                best, br = x, r
        y_olds, y_news = changed_spans(y.old, y.new)
        ytxt = " | ".join(f"{norm(o)}→{norm(n)}" for o, n in zip(y_olds, y_news))
        if best is None or br < 0.9:
            n_untouched += 1
            remains = all(norm(s) in cur for s in y_olds if norm(s))
            tag = "X未触及(旧串仍在)" if remains else "X未触及(旧串已亡:其他批次所改)"
            if not remains:
                n_gone += 1
            print(f"[{idx}] {tag} | Y: {ytxt}")
            continue
        n_line_match += 1
        x_olds, x_news = changed_spans(best.old, best.new)
        xo = [norm(s) for s in x_olds]
        hit = any(norm(s) and any(norm(s) in x_old for x_old in xo) for s in y_olds)
        if hit:
            n_span += 1
        print(f"[{idx}] {'跨度重合' if hit else '同行异改'} | Y: {ytxt}")
        print(f"      X旧跨度: {' | '.join(xo)[:140]}")
        print(f"      X新跨度: {' | '.join(norm(s) for s in x_news)[:140]}")

    print()
    print(f"Y 修订 {len(y_pairs)} 处；X 通校改动 {len(x_pairs)} 行")
    print(
        f"行级匹配 {n_line_match}（其中跨度重合 {n_span}）；X 未触及 {n_untouched}（旧串已亡 {n_gone}）"
    )
    print("→ 读表逐条裁决：同改等价 / 整句重写覆盖 / X 漏修（可反馈上游）")


if __name__ == "__main__":
    main()
