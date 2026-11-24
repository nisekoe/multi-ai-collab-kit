# -*- coding: utf-8 -*-
r"""退路门 guard：让「退得回来吗」变成一条命令，而不是临场判断。

用法
----
    python tools\guard.py start  "本轮要做什么"     # 打退路点（记起点，先收拢遗留改动）
    python tools\guard.py finish "本轮结果一句话"    # 提交本轮改动 + 记一条轮次账
    python tools\guard.py status                    # 现在有没有退路、脏不脏
    python tools\guard.py log 10                    # 最近 10 个退路点
    python tools\guard.py undo                      # 回到本轮起点（丢掉本轮全部改动）
    python tools\guard.py undo --prev               # 丢掉上一轮（HEAD~1）
    python tools\guard.py db memory                 # 数据库一致性快照（sqlite backup API）
    python tools\guard.py audit 20                  # 事后审计：哪些轮次没有退路

为什么要有它
------------
内部守则（略） 说「退得回就干」，但「退得回」必须是**事实**，不能是我嘴上的口气。
这个脚本把三件事各变成一条命令：
    1. 文本/状态  → git 提交（仓库里的每个点都能整篇退回去）
    2. 代码/配置  → 本轮真改过的文件原样另存（_撤回点\<时间>\ + MANIFEST）
    3. 数据库     → sqlite 一致性快照（不是 cp，WAL 下 cp 会漏）

状态文件（跟着仓库走，不会孤单地留在盘上）
------------------------------------------
    <repo>\工作台\工作框架\.guard\current.json    本轮起点
    <repo>\工作台\工作框架\.guard\worklog.jsonl   每轮一行：起点/终点/改了啥/怎么退

退出码
------
    0 = 有退路（或操作成功）
    1 = 没退路 / 审计发现问题
    2 = 环境或参数问题（git 不在、仓库不存在、库找不到）
    3 = 有退路，但这一轮没收尾（工作区还有未提交改动）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

PROJECT = os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(PROJECT, "tools")
GIT = os.environ.get("KIT_GIT") or shutil.which("git") or "git"

import archive_paths  # noqa: E402
_WM = archive_paths.write_mode()
# 退路 git 仓随写主开关：old＝档案库，hub＝档案库根（须已 git init）。
ROOT = str(archive_paths.old_root()) if _WM == "old" else str(archive_paths.paths.resolve_root(for_write=True)[0])
def _guard_rel(name):
    rel = os.path.join("工作台", "工作框架", ".guard", name)
    return archive_paths.map_rel(rel) if _WM == "hub" else rel
CURRENT_REL = _guard_rel("current.json")
WORKLOG_REL = _guard_rel("worklog.jsonl")
WATERMARK_REL = _guard_rel("code_watermark.txt")
CODE_STORE = os.path.join(PROJECT, "_撤回点")

import hashlib
import shutil

# ---------------------------------------------- 外部配置备份（2026-11-17 第二-6）
# 仓库只到 档案库\；代码另存只盯 tools\ + astrbot 配置。
# 但 ~\.dsh\ 下这几份（profile 的沙箱/审批/权限、飞书接入的四个默认）改了会影响
# 「我怎么运转、飞书助手怎么运转」，以前既不进 git 也不进另存——退路其实没盖住。
EXT_LIST = os.path.join(TOOLS, "guard_external.json")
EXT_DEFAULT = (
    os.path.join(os.path.expanduser("~"), ".dsh", "profiles", "desktop", "cordis.patch.yml"),
    os.path.join(os.path.expanduser("~"), ".dsh", "integrations", "dsh-feishu", "workspaces.json"),
)
EXT_STORE = os.path.join(PROJECT, "_外部快照")


def load_external_list(path: str | None = None) -> list[str]:
    """要备份的外部配置文件清单（json 数组；坏了就退回默认两条）。"""
    p = path or EXT_LIST
    try:
        with open(p, encoding="utf-8") as fh:
            rows = json.load(fh)
        rows = [x for x in rows if isinstance(x, str) and x.strip()]
        return rows or list(EXT_DEFAULT)
    except Exception:  # noqa: BLE001
        return list(EXT_DEFAULT)


def _sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(src: str) -> str:
    """把绝对路径压成一个能放回原位的文件名：盘符_目录_文件。"""
    s = src.replace("\\", "/").strip("/")
    s = s.replace(":", "")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def snapshot_external(files=None, store: str | None = None, stamp: str | None = None):
    """把外部配置文件复制进 _外部快照/<时间戳>/，写 MANIFEST；返回 (stamp, rows)。"""
    files = list(files) if files is not None else load_external_list()
    store = store or EXT_STORE
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(store, stamp)
    rows = []
    for src in files:
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dest, _safe_name(src))
        try:
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError:
            continue
        rows.append({"src": src, "copy": dst, "sha1": _sha1_file(src),
                     "bytes": os.path.getsize(src)})
    if rows:
        _write(os.path.join(dest, "MANIFEST.json"),
               json.dumps({"at": _now(), "files": rows}, ensure_ascii=False, indent=2) + "\n")
    return stamp, rows


def latest_external(store: str | None = None) -> dict:
    """最近一次外部备份的 MANIFEST（没有就返回空）。"""
    store = store or EXT_STORE
    if not os.path.isdir(store):
        return {}
    stamps = sorted(d for d in os.listdir(store) if os.path.isdir(os.path.join(store, d)))
    for stamp in reversed(stamps):
        p = os.path.join(store, stamp, "MANIFEST.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:  # noqa: BLE001
                continue
    return {}


def external_drift(files=None, store: str | None = None) -> list[dict]:
    """外部配置文件里，自上次备份后改过（或备份里没有的）那些。"""
    man = latest_external(store)
    known = {r.get("src"): r.get("sha1") for r in (man.get("files") or [])}
    out = []
    for src in (list(files) if files is not None else load_external_list()):
        if not os.path.isfile(src):
            continue
        now = _sha1_file(src)
        if known.get(src) != now:
            out.append({"src": src, "sha1": now, "backed": known.get(src)})
    return out

DB_MAP = {
    "memory": "KIT_GUARD_MEMORY_DB",
    "companion": "KIT_GUARD_COMPANION_DB",
    "data_v4": "KIT_GUARD_STATE_DB",
}

# 代码/配置：只另存「本轮真改过」的那些，限制条数与体积
CODE_WATCH = (
    os.path.join(PROJECT, "tools"),
    os.path.join(PROJECT, "config"),
    os.path.join(PROJECT, "plugins"),
)
CODE_SKIP_SUFFIX = (".bak", ".log", ".db", ".sqlite3", ".wav", ".zstd", ".pyc")
CODE_MAX_FILES = 400
CODE_MAX_BYTES = 40 * 1024 * 1024

if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
try:
    import safe_write  # noqa: E402  写文本的唯一入口（内部守则（略））
except Exception:  # noqa: BLE001
    safe_write = None


# ----------------------------------------------------------------- 基础

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _short(sha: str) -> str:
    return (sha or "")[:8]


def _p(repo: str, rel: str) -> str:
    return os.path.join(repo, rel)


def git(*args: str, repo: str = ROOT, check: bool = True):
    # quotepath=false：不然中文文件名会被 git 转成 "\345\244..." 这种八进制转义，
    # 调用方拿到的是一个"看起来像路径"的假路径（2026-11-17 用例 40 抓到：
    # 大文件因为路径读不出来被当成"没有大小"，照样进了版本库）。
    p = subprocess.run([GIT, "-c", "core.quotepath=false", *args], cwd=repo, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{(p.stderr or p.stdout).strip()[:300]}")
    return p


def head(repo: str = ROOT) -> str:
    return git("rev-parse", "HEAD", repo=repo).stdout.strip()


def dirty(repo: str = ROOT) -> list[str]:
    out = git("status", "--porcelain", repo=repo).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def big_limit() -> int:
    """进版本库的大小上限：默认 8MB，`DSH_BIG_STAGE_MB` 可改（调试与用例用）。

    2026-11-17 从 2MB 提到 8MB：流水文件会自然长到 2.7MB，那是正经档案，不该被
    挡在版本库外（当天 11-15 那份就被误挡，收工时留下未提交改动）。日志、导出包、
    临时二进制仍在别的目录，8MB 足够拦住它们。
    """
    try:
        mb = float(os.environ.get("DSH_BIG_STAGE_MB") or 8)
    except ValueError:
        mb = 8.0
    return int(mb * 1024 * 1024)


def staged_paths(repo: str = ROOT) -> list[str]:
    out = git("diff", "--cached", "--name-only", repo=repo).stdout
    return [x for x in out.splitlines() if x.strip()]


def unstage(rel: str, repo: str = ROOT) -> None:
    r = git("reset", "-q", "HEAD", "--", rel, repo=repo)
    if r.returncode != 0:          # 还没有第一个提交时 reset 会失败，退回 rm --cached
        git("rm", "--cached", "-q", "--", rel, repo=repo)


def commit_all(msg: str, repo: str = ROOT):
    """全仓收拢，但不把大件塞进版本库（2026-11-17 补 GPT 审计 A11）。

    以前是 `git add -A` 之后直接提交：日志、导出包、临时二进制一旦落在库目录里，
    就会被一起提交，仓库越滚越大且没法回滚（历史里删不掉）。
    现在先 add，再把超过上限（默认 8MB，见 big_limit）的**撤出暂存区**并明说
    跳过了哪些——它们仍在工作区，想收就自己 `git add`。
    """
    git("add", "-A", repo=repo)
    limit = big_limit()
    skipped = []
    for rel in staged_paths(repo):
        p = os.path.join(repo, rel)
        try:
            if os.path.getsize(p) > limit:
                unstage(rel, repo=repo)
                skipped.append(rel)
        except OSError:
            continue
    if skipped:
        print("跳过 %d 个大文件（>%dMB，没进版本库；要收请自己 git add）：%s%s"
              % (len(skipped), limit // (1024 * 1024),
                 "、".join(skipped[:3]), "…" if len(skipped) > 3 else ""))
    if not dirty(repo):
        return None
    git("commit", "-q", "-m", msg, repo=repo)
    return head(repo)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if safe_write is not None:
        safe_write.write_text(path, text, allow_crlf=False)
    else:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def _append(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if safe_write is not None:
        safe_write.append_text(path, text, allow_crlf=False)
    else:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def read_current(repo: str = ROOT) -> dict:
    try:
        with open(_p(repo, CURRENT_REL), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {"active": False}


def write_current(repo: str, data: dict) -> None:
    _write(_p(repo, CURRENT_REL),
           json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_worklog(repo: str, limit: int) -> list[dict]:
    path = _p(repo, WORKLOG_REL)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


# ------------------------------------------------------- 代码/配置 另存

def _watermark(repo: str) -> float:
    try:
        with open(_p(repo, WATERMARK_REL), encoding="utf-8") as fh:
            return float(fh.read().strip())
    except Exception:  # noqa: BLE001
        return 0.0


def _set_watermark(repo: str, ts: float) -> None:
    _write(_p(repo, WATERMARK_REL), f"{ts:.0f}\n")


def save_code_changes(limit_from: float, stamp: str) -> list[str]:
    """把 limit_from 之后改过的代码/配置文件原样另存，返回相对路径列表。"""
    saved: list[str] = []
    total = 0
    for base in CODE_WATCH:
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", "node_modules", ".git", "data")]
            for fn in filenames:
                if len(saved) >= CODE_MAX_FILES:
                    return saved
                if fn.lower().endswith(CODE_SKIP_SUFFIX):
                    continue
                src = os.path.join(dirpath, fn)
                try:
                    st = os.stat(src)
                except OSError:
                    continue
                if st.st_mtime <= limit_from or st.st_size == 0:
                    continue
                if total + st.st_size > CODE_MAX_BYTES:
                    return saved
                rel = os.path.relpath(src, PROJECT)
                dst = os.path.join(CODE_STORE, stamp, rel)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(src, "rb") as a, open(dst, "wb") as b:
                        b.write(a.read())
                except OSError:
                    continue
                total += st.st_size
                saved.append(rel)
    if saved:
        _write(os.path.join(CODE_STORE, stamp, "MANIFEST.json"),
               json.dumps({"at": _now(), "files": saved}, ensure_ascii=False, indent=2) + "\n")
    return saved


# ------------------------------------------------------------- 命令实现

def cmd_start(args) -> int:
    repo = args.repo
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"仓库不存在：{repo}（先跑 git init）")
        return 2
    left = dirty(repo)
    if left:
        sha = commit_all("guard: 收拢上一轮未提交的改动（%d 个文件）" % len(left), repo=repo)
        print(f"先收拢上一轮遗留的 {len(left)} 个未提交文件 → {_short(sha or '')}")
    prev = read_current(repo)
    if prev.get("active"):
        # 以前直接覆盖 current.json，上一轮"干到一半"的起点就只剩 worklog 里那行开始时间。
        # 现在把它另存一份（不进 git，同目录），谁想查"那轮到哪一步"都还能读出来。
        arch = os.path.join(os.path.dirname(_p(repo, CURRENT_REL)),
                            "未收尾-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        _write(arch, json.dumps(prev, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        print("注意：上一轮没走 finish，起点已另存：%s（旧起点仍在 git log 里）"
              % os.path.relpath(arch, repo))
    data = {
        "active": True,
        "task": args.task,
        "start_at": _now(),
        "start_commit": head(repo),
        "code_watermark": _watermark(repo) or time.time(),
    }
    write_current(repo, data)
    # 起点行也进 worklog：否则"开了工没收尾"的那一轮在审计里根本不存在
    _append(_p(repo, WORKLOG_REL), json.dumps({
        "phase": "start", "at": data["start_at"], "task": args.task,
        "start_at": data["start_at"], "start_commit": data["start_commit"],
    }, ensure_ascii=False) + "\n")
    sha = commit_all("guard: start %s" % args.task[:60], repo=repo) or data["start_commit"]
    print(f"退路已打点：{_short(sha)}｜起点 {data['start_at']}")
    print("回到此刻：python tools\\guard.py undo")
    return 0


def cmd_finish(args) -> int:
    repo = args.repo
    cur = read_current(repo)
    start_commit = cur.get("start_commit") or ""
    start_at = cur.get("start_at") or _now()
    task = cur.get("task") or ""
    wm = cur.get("code_watermark") or 0.0

    end_commit = commit_all("round: %s" % args.result[:60], repo=repo)
    files = 0
    if start_commit:
        out = git("diff", "--name-only", start_commit, "HEAD", repo=repo).stdout
        files = len([x for x in out.splitlines() if x.strip()])
    elif end_commit:
        # 没走 start（例如直接 closeout）：按本轮那个提交自己数，别报 0
        out = git("show", "--name-only", "--pretty=format:", end_commit, repo=repo).stdout
        files = len([x for x in out.splitlines() if x.strip()])
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # 没走 start 的轮次也要收代码：退到"最近一小时"，比"什么都没有"好
    wm = float(wm) or _watermark(repo) or (time.time() - 3600)
    code_saved = [] if args.no_code else save_code_changes(float(wm), stamp)
    ext_stamp, ext_rows = ("", []) if args.no_code else snapshot_external()
    if not args.no_code:
        _set_watermark(repo, time.time())

    entry = {
        "phase": "finish",
        "at": _now(),
        "task": task,
        "result": args.result,
        "start_at": start_at,
        "start_commit": start_commit,
        "end_commit": end_commit or "",
        "files_changed": files,
        "code_files_saved": len(code_saved),
        "external_files_saved": len(ext_rows),
        "how_to_undo": (f"git -C {repo} reset --hard {start_commit}" if start_commit else ""),
    }
    _append(_p(repo, WORKLOG_REL), json.dumps(entry, ensure_ascii=False) + "\n")
    write_current(repo, {"active": False, "last_at": entry["at"]})
    commit_all("guard: 记本轮退路（%d 个文本改动）" % files, repo=repo)

    print(f"本轮退路：{_short(end_commit) if end_commit else '（无文本改动）'}"
          f"｜文本改动 {files} 个｜代码/配置另存 {len(code_saved)} 个")
    if start_commit:
        print(f"怎么退：git -C {repo} reset --hard {start_commit}")
    if code_saved:
        print(f"代码另存：{os.path.join(CODE_STORE, stamp)}（按 MANIFEST 拷回即可）")
    if ext_rows:
        print(f"外部配置备份：{os.path.join(EXT_STORE, ext_stamp)}（{len(ext_rows)} 个；怎么退：把 MANIFEST 里的 copy 覆盖回 src）")
    return 0


def cmd_status(args) -> int:
    repo = args.repo
    cur = read_current(repo)
    left = dirty(repo)
    print("=== 退路门状态 ===")
    print(f"仓库：{repo}")
    print(f"当前 HEAD：{_short(head(repo))}")
    print("未提交改动：%d 个%s" % (len(left), "" if left else "（干净）"))
    try:
        drift = external_drift()
    except Exception:
        drift = []
    print("外部配置：自上次备份后变了 %d 个%s" % (len(drift), "" if not drift else "（跑 finish 会备份）"))
    if cur.get("active"):
        print(f"本轮：{cur.get('task')}｜起点 {_short(cur.get('start_commit'))}｜{cur.get('start_at')}")
        print("判定：A 有退路 → 直接干")
    else:
        print("本轮：没在跑（上一轮收尾于 %s）" % (cur.get("last_at") or "未知"))
        print("判定：A 有退路（退到 %s 就回去了）" % _short(head(repo)))
    if left:
        print("提示：有未提交改动，退路仍是上一个点；要保住当前状态先 finish")
        return 3
    return 0


def cmd_log(args) -> int:
    print(git("log", "--oneline", "-%d" % max(1, args.n), repo=args.repo).stdout.strip())
    return 0


def cmd_undo(args) -> int:
    repo = args.repo
    cur = read_current(repo)
    target = ""
    if args.prev:
        target = git("rev-parse", "HEAD~1", repo=repo).stdout.strip()
    elif cur.get("start_commit"):
        target = cur["start_commit"]
    if not target:
        print("没有可退的点（本轮没 start，也没有 --prev）")
        return 1
    before = head(repo)
    if args.dry_run:
        print(f"[dry-run] 将重置到 {_short(target)}（当前 {_short(before)}）")
        stat = git("diff", "--stat", target, "HEAD", repo=repo).stdout.strip()
        print(stat or "（两者之间没有已提交的差异）")
        # 注意：只有 -fd，没有 -x。带 -x 会连忽略文件一起删（.bak/日志都在忽略里），
        # 哪天谁加了 -x，这条 dry-run 清单就是唯一的刹车 —— 所以先列出来再动人。
        clean = git("clean", "-fdn", repo=repo).stdout.strip()
        print("将被清掉的未跟踪文件：" + (("\n" + clean) if clean else "（无）"))
        print("（--dry-run：什么都没动）")
        return 0
    git("reset", "--hard", target, repo=repo)
    # reset 只回已跟踪文件；本轮新建的文件仍是 untracked，必须一并清掉才算"退回去了"
    removed = [ln for ln in git("clean", "-fd", repo=repo).stdout.splitlines() if ln.strip()]
    print(f"已退回 {_short(target)}（原 {_short(before)}）")
    if removed:
        print(f"同时清掉本轮新建的未跟踪文件 {len(removed)} 个：")
        for ln in removed[:20]:
            print("  " + ln)
    print("误退可救：git reflog → git reset --hard " + before[:8])
    return 0


def cmd_db(args) -> int:
    setting = DB_MAP.get(args.name)
    if setting is None:
        print(f"不认识的库名 {args.name!r}，可选：{', '.join(DB_MAP)}")
        return 2
    src = os.environ.get(setting)
    if not src:
        print(f"请用 {setting} 指定 {args.name} 的 SQLite 文件。")
        return 2
    src = os.path.expanduser(src)
    if not os.path.exists(src):
        print(f"库不存在：{src}")
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst_dir = os.path.join(PROJECT, "_备份", f"{stamp}-{args.name}")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(src))
    src_con = sqlite3.connect(src)
    dst_con = sqlite3.connect(dst)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    check_db = sqlite3.connect(dst)
    try:
        check = check_db.execute("pragma quick_check").fetchone()[0]
    finally:
        check_db.close()
    print(f"库快照：{dst}")
    print(f"大小 {os.path.getsize(dst):,} 字节｜quick_check={check}｜{_now()}")
    return 0 if check == "ok" else 1


def cmd_audit(args) -> int:
    rows = read_worklog(args.repo, 10000)
    # 开了工没收尾：有 start 行、却没有 start_commit 相同的 finish 行
    finishes = [r for r in rows if r.get("phase", "finish") == "finish"]
    finished = {r.get("start_commit") for r in finishes if r.get("start_commit")}
    # 2026-11-15 修：原来拿 at 时间戳比大小，同一秒内 start 与 finish 的时戳一模一样，
    # ">" 会把刚开的工判成历史账目，audit 于是返回 0 —— test_guard 第 5 项因此一直红。
    # 改为按 worklog 行序判定：文件是追加写的，行序就是先后，不看时钟精度。
    last_finish_index = max((i for i, r in enumerate(rows)
                             if r.get("phase", "finish") == "finish"), default=-1)
    all_orphans = [(i, r) for i, r in enumerate(rows)
                   if r.get("phase") == "start" and r.get("start_commit") not in finished]
    # 最后一次 finish 之后还挂着的才算"这一轮没收尾"；更早的属于历史账目问题
    orphans = [r for i, r in all_orphans if i > last_finish_index]
    old_orphans = [r for i, r in all_orphans if i <= last_finish_index]
    rounds = finishes[-args.n:]
    print(f"=== 退路审计（最近 {len(rounds)} 轮；worklog 共 {len(rows)} 行） ===")
    bad = 0
    if old_orphans:
        print(f"（历史上有 {len(old_orphans)} 轮 start 没配对 finish，改动已被后续提交收拢，不判失败）")
    if orphans:
        print(f"！有 {len(orphans)} 轮开了工没收尾（start 了没 finish）：")
        for r in orphans[-5:]:
            print(f"   - {r.get('at')}｜{str(r.get('task', ''))[:40]}｜起点 {_short(r.get('start_commit'))}")
        bad += len(orphans)
    for i, r in enumerate(rounds, 1):
        flag = "OK"
        if r.get("files_changed", 0) > 0 and not r.get("end_commit"):
            flag = "无退路"
            bad += 1
        print(f"{i:2d}. {r.get('at')}｜改 {r.get('files_changed', 0):3d}｜"
              f"{flag}｜{str(r.get('result', ''))[:40]}")
    left = dirty(args.repo)
    if left:
        print(f"！当前还有 {len(left)} 个未提交改动（未走 finish）")
        bad += 1
    print(f"结论：{'有 ' + str(bad) + ' 处没退路' if bad else '全部有退路'}")
    return 1 if bad else 0


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="退路门：把「能不能还原」变成一条命令")
    ap.add_argument("--repo", default=ROOT, help="（测试用）仓库路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start"); p.add_argument("task"); p.set_defaults(fn=cmd_start)
    p = sub.add_parser("finish"); p.add_argument("result"); p.set_defaults(fn=cmd_finish)
    p.add_argument("--no-code", action="store_true")
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("log"); p.add_argument("n", nargs="?", type=int, default=10); p.set_defaults(fn=cmd_log)
    p = sub.add_parser("undo")
    p.add_argument("--prev", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只列出会重置到哪、会清掉什么，不动手")
    p.set_defaults(fn=cmd_undo)
    p = sub.add_parser("db"); p.add_argument("name"); p.set_defaults(fn=cmd_db)
    p = sub.add_parser("audit"); p.add_argument("n", nargs="?", type=int, default=20); p.set_defaults(fn=cmd_audit)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except RuntimeError as exc:
        print(f"! {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
