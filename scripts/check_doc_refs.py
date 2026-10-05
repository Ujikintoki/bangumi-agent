#!/usr/bin/env python3
"""文档引用检查 —— 文档里引用的仓库路径必须真实存在，且大小写与 git 一致。

为什么需要它
------------
macOS 的 APFS 默认不区分大小写，本仓库又设了 ``core.ignorecase=true``。
两个因素叠加的后果是：``docs/Eval/`` 和 ``docs/eval/`` 在你本机看起来
是同一个目录，**你永远看不到问题**；而任何大小写敏感的文件系统
（Linux / CI / GitHub 的 markdown 渲染）只认 git 索引里存的那一个。

引用目标的判定基准是 **git 索引**，不是磁盘。理由：索引才是别人 clone 到的东西，
而且在 macOS 上磁盘名字可能和索引不一致。被检查的文档则包含未跟踪 Markdown，
避免新写活文档在 ``git add`` 前得到假绿。

检查什么
--------
1. markdown 链接 ``[文字](目标)`` —— 全部检查（这是读者真正会点的）
2. 反引号里的路径 —— 只检查**以仓库真实顶层目录开头**的
   （``docs/…`` 检查；``memory/short_term.py`` 这种文件树片段跳过）

每个引用按 **相对仓库根** 和 **相对本文档所在目录** 两种解释各试一次，
任一成立即通过。

不检查 ``eval/results/`` 与 ``docs/Test/test_output/``：那是生成产物，不是文档。

四类问题
--------
CASE         索引里有，但大小写不符（Linux / GitHub 上会 404）
BROKEN       索引和磁盘都没有（真正断链）
UNTRACKED    磁盘上有，没被忽略，但没进索引 —— 多半是忘了 git add
LOCAL-ONLY   索引里没有，且路径被 .gitignore 覆盖（别人 clone 后拿不到）

UNTRACKED 和 LOCAL-ONLY 都是“别人 clone 后拿不到”，但病根不同：
前者是本地已存在但忘了 git add，后者是被忽略规则明确排除。
LOCAL-ONLY 不要求路径当前存在；这样本地工作区和干净 CI checkout 会得到相同结果。

退出码
------
0  无 CASE / BROKEN（UNTRACKED / LOCAL-ONLY / ARCHIVAL 只提示）
1  存在 CASE 或 BROKEN

已知局限
--------
忽略文件中的 Markdown 不会被扫描；它们本就不会进入其他人的 clone。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# ── 允许的例外 ────────────────────────────────────────────────
# 键 = 被引用的路径，值 = 为什么允许它不存在。
# 只放"故意指向不存在的东西"，别放"还没来得及修"。
ALLOWLIST: dict[str, str] = {
    "agent/nodes/critic.py": "CLAUDE.md 明说该文件已删除，此处是恢复指引",
    "agent/orchestrate/nodes.py": "docs/memory/README.md 用它举例说明旧路径已失效",
}

# 生成产物，不是文档，不扫
SKIP_PREFIXES = ("eval/data/", "eval/results/", "rag/corpus/", "scripts/data/", "docs/Test/test_output/")

# ── 权威层级声明 ──────────────────────────────────────────────
# 列在这里的是**活着**的文档：读者会照着做，引用必须有效，断了就失败。
# 不列的按「历史档案」处理：它记录的是当时的状态，引用当时存在、
# 现在没有了的路径属于史实，**不要改**，只提示。
#
# 这份名单是本文档体系的权威声明，改它等于改文档层级。
# 与 docs/README.md 的「文档地图」一节必须保持一致。
LIVE_PREFIXES = (
    "AGENTS.md",
    "CLAUDE.md",
    "PRODUCT.md",
    "README.md",
    "ROADMAP.md",
    ".github/",
    "docs/README.md",
    "docs/architecture-blueprint.md",
    "docs/database-admin.md",
    "docs/memory/",
    "docs/Rag/",
    "eval/README.md",
    "eval/DATA_PROVENANCE.md",
)


def is_live(md: str) -> bool:
    return any(md == p or md.startswith(p) for p in LIVE_PREFIXES)

RE_BACKTICK = re.compile(r"`([^`\n]+)`")
RE_MDLINK = re.compile(r"\]\(([^)\s]+)\)")
RE_EXT = r"\.(?:py|md|json|ini|yml|yaml|toml|txt|lock|sh|html|js|ts|css|cfg|csv)"
# 去掉 ":行号" / ":行号-行号" / ":符号名" 这类后缀
RE_SUFFIX = re.compile(rf"^(.+?{RE_EXT})(?::[^\s]*)?$")

REPO = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    capture_output=True, text=True, check=True,
).stdout.strip()


def tracked() -> tuple[set[str], set[str], set[str], dict[str, str]]:
    """git 索引里的文件、目录、顶层条目，以及小写→规范名 的映射。"""
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True, cwd=REPO,
    ).stdout
    files = {line for line in out.splitlines() if line}
    dirs: set[str] = set()
    top: set[str] = set()
    for f in files:
        parts = f.split("/")
        top.add(parts[0])
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    ci = {p.lower(): p for p in (files | dirs)}
    return files, dirs, top, ci


def disk_exact(rel: str) -> bool:
    """大小写敏感的存在性检查（逐级 listdir，不依赖文件系统语义）。"""
    cur = REPO
    for part in rel.split("/"):
        try:
            if part not in os.listdir(cur):
                return False
        except (NotADirectoryError, FileNotFoundError):
            return False
        cur = os.path.join(cur, part)
    return True


def disk_fold(rel: str) -> str | None:
    """磁盘上做大小写不敏感查找，返回真实拼写。"""
    cur, fixed = REPO, []
    for part in rel.split("/"):
        try:
            entries = os.listdir(cur)
        except (NotADirectoryError, FileNotFoundError):
            return None
        hit = next((e for e in entries if e.lower() == part.lower()), None)
        if hit is None:
            return None
        fixed.append(hit)
        cur = os.path.join(cur, hit)
    return "/".join(fixed)


_IGNORED_CACHE: dict[str, bool] = {}


def ignored(rel: str) -> bool:
    """这个路径被 .gitignore 挡着吗？（结果缓存）

    对尚未存在的目录同时检查带尾斜杠形式；否则 ``foo/`` 这类
    目录规则在干净 checkout 中不会匹配候选 ``foo``。
    """
    if rel not in _IGNORED_CACHE:
        candidates = (rel, f"{rel.rstrip('/')}/")
        _IGNORED_CACHE[rel] = any(
            subprocess.run(
                ["git", "check-ignore", "-q", candidate], cwd=REPO, check=False,
            ).returncode == 0
            for candidate in candidates
        )
    return _IGNORED_CACHE[rel]


def clean(ref: str) -> str | None:
    """规范化候选引用；不像路径就返回 None。"""
    if not ref or len(ref) > 200:
        return None
    if ref.startswith(("http://", "https://", "#", "mailto:", "//", "-")):
        return None
    if any(c in ref for c in " \t*<>{}|\\^$`"):
        return None
    m = RE_SUFFIX.match(ref)
    if m:
        ref = m.group(1)
    ref = ref.rstrip("/")
    if not ref:
        return None
    # 保留开头的 "../"，交给 classify 按文档所在目录解析
    if ref.startswith("../"):
        return ref
    if ".." in ref.split("/"):
        return None
    return ref.lstrip("./")


def refs_in(line: str, top_level: set[str]) -> set[str]:
    out: set[str] = set()
    for m in RE_MDLINK.finditer(line):
        c = clean(m.group(1))
        # 真链接一定带 "." 或 "/"；这条挡掉文档里讲解语法用的 `[文字](目标)`
        if c and ("/" in c or "." in c):
            out.add(c)
    for m in RE_BACKTICK.finditer(line):
        c = clean(m.group(1))
        # 反引号只认完整路径，否则文件树片段会淹没结果
        if c and "/" in c and c.split("/")[0] in top_level:
            out.add(c)
    return out


def classify(ref: str, doc_dir: str, files, dirs, ci):
    """→ (状态, 补充信息)。状态 ∈ OK / CASE / BROKEN / UNTRACKED / LOCAL-ONLY。"""
    cands = [ref]
    if doc_dir:
        cands.append(f"{doc_dir}/{ref}")
    if ref.startswith("../"):
        # 相对父目录的写法：按本文档所在目录解析，且不许逃出仓库
        if not doc_dir:
            return "BROKEN", ""
        resolved = os.path.normpath(os.path.join(doc_dir, ref))
        if resolved.startswith(".."):
            return "BROKEN", ""
        cands = [resolved]
    else:
        cands = [c for c in cands if ".." not in c.split("/")]
    for c in cands:
        if c in files or c in dirs:
            return "OK", c
    for c in cands:
        canon = ci.get(c.lower())
        if canon is not None:
            return "CASE", canon
    for c in cands:
        on_disk = c if disk_exact(c) else disk_fold(c)
        if on_disk:
            # 磁盘上有但索引里没有，是两种不同的病：
            #   被 .gitignore 挡着 → 故意的，别人 clone 拿不到（LOCAL-ONLY）
            #   没被忽略        → 忘了 git add（UNTRACKED）
            return ("LOCAL-ONLY" if ignored(on_disk) else "UNTRACKED"), on_disk
    # 干净 checkout 中被忽略的生成目录通常尚不存在，但它们仍然不是
    # “意外断链”。直接用 git 的忽略规则判定，保证本地与 CI 分类一致。
    for c in cands:
        if ignored(c):
            return "LOCAL-ONLY", c
    return "BROKEN", ""


def main() -> int:
    files, dirs, top_level, ci = tracked()
    untracked_md = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "*.md"],
        capture_output=True, text=True, cwd=REPO, check=True,
    ).stdout.split()
    md_files = sorted(
        f for f in files | set(untracked_md)
        if f.endswith(".md") and not f.startswith(SKIP_PREFIXES)
    )

    buckets: dict[str, list[tuple[str, int, str, str]]] = {
        "CASE": [], "BROKEN": [], "UNTRACKED": [], "LOCAL-ONLY": [], "ARCHIVAL": [],
    }

    for md in md_files:
        try:
            with open(os.path.join(REPO, md), encoding="utf-8") as stream:
                lines = stream.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        doc_dir = os.path.dirname(md)
        live = is_live(md)
        for lineno, line in enumerate(lines, 1):
            for ref in refs_in(line, top_level):
                if ref in ALLOWLIST:
                    continue
                status, extra = classify(ref, doc_dir, files, dirs, ci)
                if status == "OK":
                    continue
                # 大小写错误永远失败：它不是史实，它在 Linux 上就是坏的
                if status == "BROKEN" and not live:
                    status = "ARCHIVAL"
                buckets[status].append((md, lineno, ref, extra))

    titles = {
        "CASE": "CASE — 大小写与 git 不符（Linux / GitHub 上会 404）",
        "BROKEN": "BROKEN — 活文档里的断链",
        "UNTRACKED": "UNTRACKED — 文件在磁盘上但没入库（多半是忘了 git add）",
        "LOCAL-ONLY": "LOCAL-ONLY — 被 .gitignore 挡着，别人 clone 后拿不到",
        "ARCHIVAL": "ARCHIVAL — 历史档案引用当时的路径（史实，不要改）",
    }
    hints = {
        "CASE": "修法：按「索引里是」改写引用",
        "BROKEN": "修法：改正路径，或加进 ALLOWLIST 并写明理由",
        "UNTRACKED": "提示项，不失败。确认该入库就 git add，否则加进 .gitignore",
        "LOCAL-ONLY": "提示项，不失败。若是给读者看的，应移出 gitignore",
        "ARCHIVAL": "提示项，不失败。要让它有效，就把文档加进 LIVE_PREFIXES",
    }
    for key in ("CASE", "BROKEN", "UNTRACKED", "LOCAL-ONLY", "ARCHIVAL"):
        rows = buckets[key]
        if not rows:
            continue
        print(f"\n{'=' * 72}\n{titles[key]}  ({len(rows)})\n{'=' * 72}")
        # 按文件聚合，避免同一个文件刷屏
        by_file: dict[str, list[tuple[int, str, str]]] = {}
        for md, lineno, ref, extra in rows:
            by_file.setdefault(md, []).append((lineno, ref, extra))
        for md, items in sorted(by_file.items()):
            print(f"  {md}  ({len(items)})")
            for lineno, ref, extra in items:
                tail = f"   → 索引里是 {extra}" if extra and key == "CASE" else ""
                print(f"      :{lineno:<4} {ref}{tail}")
        print(f"  {hints[key]}")

    n_case, n_broken = len(buckets["CASE"]), len(buckets["BROKEN"])
    print(f"\n{'=' * 72}")
    print(f"扫描 {len(md_files)} 份 .md（含 {len(untracked_md)} 份未跟踪文档；"
          f"活文档 {sum(1 for m in md_files if is_live(m))} / "
          f"历史档案 {sum(1 for m in md_files if not is_live(m))}）")
    print(f"  ❌ CASE {n_case} / BROKEN {n_broken}   ← 失败项")
    print(f"  ⚠  UNTRACKED {len(buckets['UNTRACKED'])}"
          f" / LOCAL-ONLY {len(buckets['LOCAL-ONLY'])}"
          f" / ARCHIVAL {len(buckets['ARCHIVAL'])}   ← 提示项")
    if not n_case and not n_broken:
        print("✅ 活文档的引用全部有效，大小写全部正确")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
