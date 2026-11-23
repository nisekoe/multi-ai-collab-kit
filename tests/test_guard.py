
# -*- coding: utf-8 -*-
"""退路门（guard.py）回归测试：临时仓库跑真 git，不碰正本仓库。

判据（每条都是跑出来的结果，不是"看着对"）：
  1 start 后 current.json.active=True、start_commit 非空，且 worklog 出现 phase=start 行
  2 仓库脏时 start 先把遗留改动收拢成一个提交（不丢东西）
  3 finish 后工作区干净、worklog 有一行 finish、files_changed>0、end_commit 非空
  4 audit 对"每轮都有退路"返回 0
  5 "start 了没 finish" 必须被 audit 抓出来（飞书助手 2026-11-15 review 指出的盲区）
  6 undo --dry-run 只列清单、不动手（文件还在、HEAD 不变）
  7 undo 能退回本轮起点（本轮新建的文件消失）
  8 status：干净=0、脏=3（3 = 有退路但没收尾，不是"没退路"）
  9 不认识库名 → 2（不瞎猜路径）
  10/11 同一秒的 worklog：finish 之后的未收尾必须报、配对完的不许报（钉死"按行序不按时间"）
"""
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

if __name__ == "__main__":

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    SPEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard.py")
    GIT = r"C:\Program Files\Git\cmd\git.exe"
    spec = importlib.util.spec_from_file_location("guard_under_test", SPEC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fails = []


    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (("｜" + detail) if detail else ""))
        if not ok:
            fails.append(name)


    def run(*args):
        return mod.main(list(args))


    def rows_of(repo):
        return mod.read_worklog(repo, 1000)


    tmp = tempfile.mkdtemp(prefix="guard_test_")
    try:
        subprocess.run([GIT, "init", "-q", tmp], check=True)
        subprocess.run([GIT, "-C", tmp, "config", "user.email", "t@t"], check=True)
        subprocess.run([GIT, "-C", tmp, "config", "user.name", "t"], check=True)
        with open(os.path.join(tmp, "base.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("base\n")
        subprocess.run([GIT, "-C", tmp, "add", "-A"], check=True)
        subprocess.run([GIT, "-C", tmp, "commit", "-q", "-m", "init"], check=True)

        # 1 start
        rc = run("--repo", tmp, "start", "测试轮")
        cur = mod.read_current(tmp)
        starts = [r for r in rows_of(tmp) if r.get("phase") == "start"]
        check("1 start 打点＋worklog 有 start 行", rc == 0 and cur.get("active") is True
              and len(cur.get("start_commit") or "") == 40 and len(starts) == 1,
              f"rc={rc} starts={len(starts)}")

        # 2 脏仓库 start 先收拢
        with open(os.path.join(tmp, "leftover.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("上一轮遗留\n")
        n_before = int(subprocess.run([GIT, "-C", tmp, "rev-list", "--count", "HEAD"],
                                      capture_output=True, text=True).stdout.strip())
        rc = run("--repo", tmp, "start", "第二轮")
        n_after = int(subprocess.run([GIT, "-C", tmp, "rev-list", "--count", "HEAD"],
                                     capture_output=True, text=True).stdout.strip())
        tracked = subprocess.run([GIT, "-C", tmp, "ls-files"], capture_output=True,
                                 text=True).stdout
        check("2 脏仓库 start 先收拢遗留", rc == 0 and n_after >= n_before + 2
              and "leftover.txt" in tracked, f"{n_before}→{n_after}")

        # 3 finish
        with open(os.path.join(tmp, "round.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("本轮产出\n")
        rc = run("--repo", tmp, "finish", "测试收尾", "--no-code")
        finishes = [r for r in rows_of(tmp) if r.get("phase") == "finish"]
        clean = subprocess.run([GIT, "-C", tmp, "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip() == ""
        check("3 finish 提交＋记账", rc == 0 and clean and len(finishes) == 1
              and finishes[0].get("end_commit") and finishes[0].get("files_changed", 0) > 0,
              f"rc={rc} clean={clean} files={finishes[0].get('files_changed') if finishes else '-'}")

        # 4 audit 全有退路
        rc = run("--repo", tmp, "audit", "5")
        check("4 audit 全有退路=0", rc == 0, f"rc={rc}")

        # 5 开了工没收尾 → audit 必须报
        run("--repo", tmp, "start", "只开不收的轮次")
        rc_orphan = run("--repo", tmp, "audit", "5")
        check("5 开工没收尾被审计抓到", rc_orphan == 1, f"rc={rc_orphan}")
        run("--repo", tmp, "finish", "补收尾", "--no-code")   # 收掉，避免影响后面

        # 6 undo --dry-run 不动手
        run("--repo", tmp, "start", "第三轮")
        probe = os.path.join(tmp, "probe.txt")
        with open(probe, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("这行该被退掉\n")
        head_before = subprocess.run([GIT, "-C", tmp, "rev-parse", "HEAD"],
                                     capture_output=True, text=True).stdout.strip()
        rc_dry = run("--repo", tmp, "undo", "--dry-run")
        head_after = subprocess.run([GIT, "-C", tmp, "rev-parse", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
        check("6 undo --dry-run 不动手", rc_dry == 0 and os.path.exists(probe)
              and head_before == head_after, f"rc={rc_dry} exists={os.path.exists(probe)}")

        # 7 undo 真退
        rc2 = run("--repo", tmp, "undo")
        check("7 undo 退回起点", rc2 == 0 and not os.path.exists(probe),
              f"rc={rc2} exists={os.path.exists(probe)}")

        # 8 status
        rc_clean = run("--repo", tmp, "status")
        with open(os.path.join(tmp, "dirty.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("脏\n")
        rc_dirty = run("--repo", tmp, "status")
        check("8 status 干净=0 脏=3", rc_clean == 0 and rc_dirty == 3,
              f"{rc_clean}/{rc_dirty}")

        # 9 不认识库名
        rc = run("--repo", tmp, "db", "不存在的库")
        check("9 不认识库名=2", rc == 2, f"rc={rc}")

        # ---- 10/11：同一秒的 worklog —— 钉住"按行序、不看时钟"（2026-11-15 修） ----
        # 先把工作区弄干净，免得 audit 的"脏"分支混进来
        with open(os.path.join(tmp, "cleanup.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("x\n")
        subprocess.run([GIT, "-C", tmp, "add", "-A"], check=True)
        subprocess.run([GIT, "-C", tmp, "commit", "-q", "-m", "clean for audit tests"], check=True)

        worklog = os.path.join(tmp, "工作台", "工作框架", ".guard", "worklog.jsonl")
        SAME = "2026-11-15 07:00:00"   # 三行故意同一秒

        def write_worklog(rows):
            os.makedirs(os.path.dirname(worklog), exist_ok=True)
            with io.open(worklog, "w", encoding="utf-8", newline="\n") as fh:
                for r in rows:
                    fh.write(__import__("json").dumps(r, ensure_ascii=False) + "\n")
            # 马上提交：否则工作区变脏，audit 的"脏"分支也会返回 1，
            # 两项就分不清是"抓到未收尾"还是"工作区脏"
            subprocess.run([GIT, "-C", tmp, "add", "-A"], check=True)
            subprocess.run([GIT, "-C", tmp, "commit", "-q", "-m", "test worklog"], check=True)

        PAIRED = {"phase": "finish", "at": SAME, "start_commit": "a" * 40,
                  "end_commit": "b" * 40, "files_changed": 1, "result": "配对的一轮"}

        write_worklog([{"phase": "start", "at": SAME, "start_commit": "a" * 40, "task": "先开的"},
                       PAIRED,
                       {"phase": "start", "at": SAME, "start_commit": "c" * 40, "task": "后开的没收尾"}])
        rc = run("--repo", tmp, "audit", "5")
        check("10 同一秒：finish 之后的未收尾必须报=1", rc == 1, f"rc={rc}")

        write_worklog([{"phase": "start", "at": SAME, "start_commit": "a" * 40, "task": "先开的"},
                       PAIRED])
        rc = run("--repo", tmp, "audit", "5")
        check("11 同一秒：都配对完了=0", rc == 0, f"rc={rc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("FAILED:", fails or "none")
    sys.exit(1 if fails else 0)