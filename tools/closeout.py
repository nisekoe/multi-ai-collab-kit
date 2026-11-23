"""收工归档：一条命令干完三件事，并打出下一轮怎么接。

用法（最简）：
    python closeout.py "这轮干了什么"

可选：
    python closeout.py "这轮干了什么" "下一步要干什么"
    python closeout.py "..." "..." --dry-run

它会做三件：
1. 往 工作台结果.md 追加一行账（走 ledger_lock：独占锁＋程序打时间戳）；
2. 打退路点（guard finish）；
3. 跑 sync_check 自检，并打印结果。
最后打印「收工三步」和一段可直接贴进新会话的开工话。

退出码（2026-11-17 第二批起）：
    0 = 收工完成
    1 = 有硬错（摘要对账／自检 报 1），这一轮不算收工
    2 = 账本没写进去（最要紧的一件），先别继续
    3 = **某个子步骤的退出码未知**（被信号杀 -9、超时、崩了给 3 之类），
        不能当"没报错"，这一轮同样不算收工

2026-11-18 起（G-04 接入）：写账本之前多跑一次只读采样 tools\truth_kernel.py。
- 它报 F（机器上的实际状态和批准的意图对不上）按硬错处理：一个字节都不写，修完再收；
- 它只报 U（验不了，例如身份缓存过期、任务没被批过）不拦，但会列出哪几项没验收；
- 采样器自己的退出码（0/1/2）和这里的三档不是一回事，所以读它的 --json 逐项判，
  不拿退出码直接套 classify_step——否则内核内部出错会被误当成 F 拦掉收工。

2026-11-16 晚改版（交接体系整合）：
- **不再写 `共享状态.md`、不再重写 `交接卡.md`**（两份已作废，封存在 `档案库\\封存\\20261116-旧交接体系\\`）；
- 手边只认「两本临时本子 + 待处理事项 + 账本」，本脚本**只读本子、不改本子**
  （改本子是人的判断：干完的删、搁置的挪进待处理事项，不自动化）。
"""
from __future__ import annotations

import io
import json
import os
import time
import re
import subprocess
import sys
import tempfile
from datetime import datetime

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

# 让 stdout/stderr 能输出任意 Unicode，避免打印 sync_check 输出时被 GBK 编码卡崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import archive_paths  # noqa: E402
WB = os.path.join(os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "档案库", "工作台")  # 遗留兼容（派生路径已逐个随写开关解析）
ENTRY = str(archive_paths.resolve("入口.md", for_write=False))
LEDGER = str(archive_paths.resolve("工作台/工作台结果.md", for_write=True))
SCRATCH = str(archive_paths.resolve("工作台/临时本子-桌面助手.md", for_write=True))
PENDING = str(archive_paths.resolve("工作台/待处理事项.md", for_write=True))
PY = sys.executable
CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_check.py")
LEDGER_LOCK = os.path.join(TOOLS, "ledger_lock.py")
GUARD = os.path.join(TOOLS, "guard.py")
REFRESH = os.path.join(TOOLS, "refresh_all.py")
SUMMARY_CHECK = os.path.join(TOOLS, "summary_check.py")
RULES_CHECK = os.path.join(TOOLS, "rules_check.py")
CATALOG = os.path.join(TOOLS, "catalog_check.py")
KERNEL = os.path.join(TOOLS, "truth_kernel.py")
DRIFT = os.path.join(TOOLS, "config_drift.py")
ID_CHECK = os.path.join(TOOLS, "id_check.py")
IMAGE_BUDGET = os.path.join(TOOLS, "image_budget.py")
PLAN_GATE = os.path.join(TOOLS, "plan_gate.py")
KERNEL_LOG = os.path.join(os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "truth-kernel.jsonl")

# ---------------------------------------------------------------- 退出码四档
# 2026-11-17 第二批（第二-1）：以前只认 0／1／2，别的码（被信号杀是负数、
# 超时或崩了可能给 3 或 None）一律当"没报错"——于是收工会说已完成，其实某一步
# 根本没跑完。这里把"未知"单列一档：不许当成功，收工判 3（本轮不算收工）。
STEP_OK, STEP_WARN, STEP_HARD, STEP_UNKNOWN = "ok", "warn", "hard", "unknown"
_KNOWN_CAUSE = {0: STEP_OK, 2: STEP_WARN, 1: STEP_HARD}


def classify_step(rc) -> str:
    """子步骤退出码 → 四档。0 全绿／2 只有已知警告／1 硬错／其它一律未知。"""
    try:
        return _KNOWN_CAUSE.get(int(rc), STEP_UNKNOWN)
    except (TypeError, ValueError):
        return STEP_UNKNOWN


RED_LINES = (
    "红线：不自建定时唤醒；不给 AstrBot 千帆 tokenplan key；"
    "不动 companion 的 voice_reply；不整读超 30KB 的文档。\n"
)


def build_block(todo: str, now: datetime) -> str:
    return (
        f"读 {ENTRY} → 自己的临时本子 → 工作台结果.md 尾 10 行，接上。\n\n"
        f"上一轮收工（{now:%Y-%m-%d %H:%M}）：\n"
        f"待做：{todo}\n"
        + RED_LINES
    )


def scratch_items() -> list[str]:
    """读临时本子，把还挂着的条目列出来（只读，不改）。"""
    try:
        with io.open(SCRATCH, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip().startswith("- [")]


def run_sample():
    """0.75) 只读采样（G-04）：返回 (档位, 要打印的行, JSON 负载)。

    采样器自己的退出码是 0=全 P／1=F 或内核自身出错／2=有 U 或采样源中途变了，
    和 classify_step 的三档不是一回事，所以不拿退出码判，改读 --json 逐项判：
      有 F   → STEP_HARD（先修再收工，账本一个字节都不写）
      只有 U → STEP_WARN（照常收工，但列出哪几项没验收）
      读不懂／超时／缺脚本 → STEP_UNKNOWN（不许当通过，也不许当 F）
    """
    if not os.path.exists(KERNEL):
        return STEP_HARD, [
            "! 采样器缺失：%s" % KERNEL,
            "  不能把「没跑」当「通过」；补回脚本再收工",
        ], {}
    try:
        # 强制子进程用 UTF-8 输出：源文件路径里有中文，管道下按系统代码页会乱码，
        # 乱码就等于读不懂 JSON（会被判未知，白拦一轮收工）。
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        pr = subprocess.run([PY, KERNEL, "--json"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return STEP_UNKNOWN, ["! 采样超时（120 秒）：它没给出结论，这一轮不算收工"], {}
    except OSError as exc:
        return STEP_UNKNOWN, ["! 采样器没跑起来（%s）：这一轮不算收工" % exc], {}
    try:
        payload = json.loads(pr.stdout or "")
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), dict):
        return STEP_UNKNOWN, [
            "! 采样输出读不懂（退出码 %s）：它没给出结论，不能当通过" % pr.returncode,
            "  单独跑 python tools\\truth_kernel.py 看它到底怎么了",
        ], {}
    checks = payload["checks"]
    rows, fails, unverified, odd = [], [], [], []
    for name in ("binding", "target", "dailycare", "listener"):
        item = checks.get(name)
        if not isinstance(item, dict):
            odd.append(name + "(缺)")
            continue
        st = item.get("status")
        reason = item.get("reason") or ""
        rows.append("  %s=%s(%s)" % (name, st, reason))
        if st == "F":
            fails.append("%s：%s" % (name, reason))
        elif st == "U":
            unverified.append("%s：%s" % (name, reason))
        elif st != "P":
            odd.append("%s(%s)" % (name, st))
    facts = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
    if facts.get("source_stable") is not True:
        unverified.append("采样源在取样期间变了（前后哈希不一致）")
    lines = rows
    if fails:
        lines += ["  对不上："] + ["    - " + x for x in fails]
        lines += [
            "  ! 采样报 F：机器上的实际状态和批准的意图对不上，先修再收工",
            "    任务被改了：改回去；若是有意改的：改 档案库\\工作台\\采样器批准意图.json",
        ]
        return STEP_HARD, lines, payload
    if odd:
        lines += ["! 采样结果里有认不得的状态：%s" % "、".join(odd)]
        return STEP_UNKNOWN, lines, payload
    if unverified:
        lines += ["  未验收："] + ["    - " + x for x in unverified]
        lines += ["  结论：U（有 %d 项验不了，不是「验过了」）" % len(unverified)]
        return STEP_WARN, lines, payload
    lines += ["  结论：P（配置说的和机器上跑的都对得上）"]
    return STEP_OK, lines, payload


def record_sample(payload, tier):
    """采样留痕：一行 JSON 进 logs\truth-kernel.jsonl（写失败不拦收工）。"""
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    row = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tier": tier,
        "kernel_exit": payload.get("kernel_exit"),
        "run_id": payload.get("run_id"),
        "checks": {
            k: (v.get("status") if isinstance(v, dict) else None)
            for k, v in checks.items()
        },
        "stable": (payload.get("facts") or {}).get("source_stable")
        if isinstance(payload.get("facts"), dict) else None,
    }
    try:
        import safe_write
        os.makedirs(os.path.dirname(KERNEL_LOG), exist_ok=True)
        if os.path.exists(KERNEL_LOG) and os.path.getsize(KERNEL_LOG) > 2000000:
            os.replace(KERNEL_LOG, KERNEL_LOG + ".1")
        safe_write.append_text(KERNEL_LOG, json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        print("  （采样留痕没写成：%s；不影响收工）" % exc)


def run_memory_export():
    """0.79) 把记忆库重导一次，让她下一次检索就能读到当前。返回一段话，不阻断收工。"""
    tool = os.path.join(TOOLS, "memory_export.py")
    if not os.path.exists(tool):
        return "（导出工具不在，跳过）"
    t0 = time.time()
    try:
        pr = subprocess.run([PY, tool], capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=180)
    except Exception as exc:  # noqa: BLE001
        return "（导出跑不起来：%s）" % exc
    out = (pr.stdout or "").strip()
    if pr.returncode != 0:
        return "导出失败（exit %s）：%s" % (pr.returncode, (pr.stderr or out).strip()[:300])
    return "%s\n用时 %.1f 秒" % (out, time.time() - t0)


def run_candidates():
    """0.78) 坑候选：到 3 次没进坑库就提醒。返回一段话，不阻断收工。"""
    tool = os.path.join(TOOLS, "pit_candidate.py")
    if not os.path.exists(tool):
        return "（候选计数工具不在，跳过）"
    try:
        pr = subprocess.run([PY, tool, "check"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=60)
    except Exception as exc:  # noqa: BLE001
        return "（候选计数跑不起来：%s）" % exc
    return (pr.stdout or "").strip() or "（无输出）"


def run_drift():
    """0.77) 配置漂移（G-04 五行）：返回 (档位, 要打印的行)。

    和 run_sample 一样：不信任脚本退出码，只认它吐出来的 JSON 结论。
    P 全过 → OK；有 F → HARD（拦）；有 U 或缺表 → WARN。
    """
    if not os.path.exists(DRIFT):
        return STEP_WARN, ["! 漂移检查缺失：%s（跳过，但别当验过了）" % DRIFT]
    try:
        pr = subprocess.run([PY, DRIFT, "--json"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                            timeout=120)
    except Exception as exc:  # noqa: BLE001
        return STEP_WARN, ["! 漂移检查跑不起来（%s: %s）" % (type(exc).__name__, exc)]
    raw = (pr.stdout or "").strip()
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return STEP_WARN, ["! 漂移检查没吐 JSON（退出码 %s）；按没验过算" % pr.returncode]
    if not data.get("ok"):
        return STEP_WARN, ["! 漂移检查跑不了：%s" % data.get("error")]
    lines, stats = [], data.get("stats") or {}
    for row in data.get("rows") or []:
        mark = {"P": "  OK  ", "F": " FAIL ", "U": "  ?   "}[row.get("status")]
        lines.append("%s%s．%s｜%s" % (mark, row.get("id"), row.get("title") or "",
                                      row.get("reason")))
        if row.get("status") == "P":
            continue
        d = row.get("detail") or {}
        for k in ("missing", "extra", "wrong", "why", "runtime", "config"):
            v = d.get(k)
            if v:
                lines.append("        %s：%s" % (k, json.dumps(v, ensure_ascii=False)[:200]))
    lines.append("  合计 P %s／F %s／U %s"
                 % (stats.get("P", 0), stats.get("F", 0), stats.get("U", 0)))
    if stats.get("F"):
        return STEP_HARD, lines
    if stats.get("U"):
        return STEP_WARN, lines
    return STEP_OK, lines


def run_gate(label, argv, hard_on=1, warn_on=(), timeout=90):
    """跑一个坑库闸，返回 (档位, 打印行)。

    hard_on／warn_on 是"这个脚本的哪个退出码算硬错／算警告"：
    各工具的码含义不一样（0 过、1 不过、2 读不了），不能一把尺子量。
    hard_on=None 表示这个闸从不硬拦（H4 图片预算就是：它拦不住已经发出去的请求）。
    """
    path = argv[0]
    if not os.path.exists(path):
        return STEP_HARD, ["! %s 缺失：%s" % (label, path),
                           "  不能把「没跑」当「通过」；补回脚本再收工"]
    try:
        r = subprocess.run([PY] + argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    except subprocess.TimeoutExpired:
        return STEP_UNKNOWN, ["! %s 超时（%s 秒）：没给出结论" % (label, timeout)]
    except OSError as exc:
        return STEP_UNKNOWN, ["! %s 没跑起来：%s" % (label, exc)]
    lines = [x for x in (r.stdout or "").strip().splitlines() if x.strip()]
    rc = r.returncode
    if rc == 0:
        tier = STEP_OK
    elif hard_on is not None and rc == hard_on:
        tier = STEP_HARD
    elif rc in warn_on:
        tier = STEP_WARN
    else:
        tier = STEP_UNKNOWN
    return tier, lines


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        print(__doc__)
        return 1
    did = args[0].strip()
    todo = args[1].strip() if len(args) > 1 else "（看临时本子的断点／待发条，和待处理事项里用户点的那条）"
    now = datetime.now()
    block = build_block(todo, now)
    # did 里若已经带了结果词（成/没成/进行中），就不要再补一遍
    tail = "→ 收工归档 → 成"
    if re.search(r"(?:成|没成|进行中)\s*$", did):
        tail = "→ 收工归档"
    entry = f"{did} {tail}"
    items = scratch_items()

    print("=== 将要写入 ===")
    print(f"[账本] {entry}   （行首时间戳由 ledger_lock 用系统时钟打，不由本脚本写）")
    print(f"[手边] 临时本子还有 {len(items)} 条：")
    for it in items:
        print(f"    {it}")
    print("[下一轮开工]")
    print(block)

    if dry:
        print("（--dry-run，未落盘）")
        return 0

    # 0.5) 摘要层对账单：放在写账本之前——硬错时一个字节都不写
    #    只读、不依赖账本，提前跑没有副作用。普通模式 2 = 只有警告，可收工。
    #   --strict 不进收工链路：摘要降级是历史稿问题，升成 FAIL 会天天报红，闸会变噪音。
    #   要严格审计就单独跑：python tools\summary_check.py --strict
    if os.path.exists(SUMMARY_CHECK):
        sr = subprocess.run([PY, SUMMARY_CHECK], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        print("\n=== 摘要对账 ===")
        lines = [x for x in (sr.stdout or "").strip().splitlines() if x.strip()]
        # 硬错明细在汇总之前，只打尾部看不到是哪一项 —— 先把 [FAIL] 全打出来
        fails = [x for x in lines if x.lstrip().startswith("[FAIL]")]
        for x in fails:
            print("  " + x)
        for x in lines[-2:]:
            print("  " + x)
        tier = classify_step(sr.returncode)
        if tier == STEP_HARD:
            print("  ! 有硬错：摘要写的数字/路径/编号和正本对不上，不能收工（改完再收）")
            return 1
        if tier == STEP_UNKNOWN:
            print("  ! 摘要对账退出码未知（%s）：它没给出结论，不能当通过" % sr.returncode)
            print("    这一轮不算收工；单独跑 python tools\\summary_check.py 看它到底怎么了")
            return 3

    # 0.6) 守则校验（2026-11-17 第二批 第二-2）：守则被悄悄改、清单和现实对不齐，
    #      以前只有人想起来才跑；现在收工前跑一次。只读、不依赖账本。
    #      硬错（1）拦收工；提示（警告）不拦；其它码＝未知，不许当通过。
    if os.path.exists(RULES_CHECK):
        ru = subprocess.run([PY, RULES_CHECK], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        print("\n=== 守则校验 ===")
        lines = [x for x in (ru.stdout or "").strip().splitlines() if x.strip()]
        print("\n".join(lines[-4:]) if lines else "（没有输出）")
        tier = classify_step(ru.returncode)
        if tier == STEP_HARD:
            print("  ! 守则对不上（被改过／没登记／清单在骗人），不能收工；先跑 tools\\rules_check.py 看明细")
            return 1
        if tier == STEP_UNKNOWN:
            print("  ! 守则校验退出码未知（%s）：它没给出结论，这一轮不算收工" % ru.returncode)
            return 3

    # 0.7) 清单体检（2026-11-17 加）：待处理/坑库只加不减、状态越区、条数声明漂了，
    #      收工前就挡。只读；硬错拦收工，未知码不算通过。
    #      2026-11-17 晚：检查器缺失必须 fail-closed（以前 exists 才跑，删掉就静默放行）；
    #      输出不再只截末三行，多错要全看见。
    if not os.path.exists(CATALOG):
        print("\n=== 清单体检 ===")
        print("  ! 检查器缺失：%s" % CATALOG)
        print("  不能把「没跑」当「通过」；补回脚本再收工")
        return 1
    ca = subprocess.run([PY, CATALOG], capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    print("\n=== 清单体检 ===")
    lines = [x for x in (ca.stdout or "").strip().splitlines() if x.strip()]
    print("\n".join(lines) if lines else "（没有输出）")
    tier = classify_step(ca.returncode)
    if tier == STEP_HARD:
        print("  ! 清单对不上（重号／状态越区／条数声明不符），不能收工；先跑 tools\\catalog_check.py 看明细")
        return 1
    if tier == STEP_UNKNOWN:
        print("  ! 清单体检退出码未知（%s）：这一轮不算收工" % ca.returncode)
        return 3

    # 0.72) 坑库闸（2026-11-18 接入：B5／H4／F3）
    #      B5 事件号查重：同号两份文件 → 硬拦（按号查会选错那份）。
    #      F3 一页纸：新工具没有批过的一页纸 → 硬拦。
    #      H4 图片预算：只警告——400 发生在模型接口那边，收工拦不住已发出的请求。
    print("\n=== 坑库闸（B5 事件号／F3 一页纸／H4 图片）===")
    gate_plan = ("B5 事件号查重", [ID_CHECK], 1, ())
    gate_tiers = {}
    hard_gates, unknown_gates, warn_gates = [], [], []
    for label, argv, hard_on, warn_on in (
            gate_plan,
            ("F3 一页纸", [PLAN_GATE, "audit"], 1, ()),
            ("H4 图片预算", [IMAGE_BUDGET], None, (1,))):
        tier, lines = run_gate(label, argv, hard_on, warn_on)
        gate_tiers[label] = tier
        print("--- %s ---" % label)
        for line in lines:
            print("  " + line)
        if tier == STEP_HARD:
            hard_gates.append(label)
        elif tier == STEP_UNKNOWN:
            unknown_gates.append(label)
        elif tier == STEP_WARN:
            warn_gates.append(label)
    if hard_gates:
        print("  ! 闸没过：%s —— 修完再收（账本一个字节都没写）" % "、".join(hard_gates))
        return 1
    if unknown_gates:
        print("  ! 这几道闸没给出结论：%s —— 这一轮不算收工" % "、".join(unknown_gates))
        print("    单独跑 python tools\\id_check.py ／ plan_gate.py audit ／ image_budget.py 看明细")
        return 3
    if warn_gates:
        print("  （%s 超了：只是提醒，不拦记账；按上面的退路换窗再继续）" % "、".join(warn_gates))
    if gate_tiers.get("F3 一页纸") == STEP_OK:
        try:
            import plan_gate as PG
            fresh = PG.new_tools()
            if fresh:
                PG.save_baseline(PG.current_tools())
                print("  台账已更新：新工具 %d 个记进去了（%s）"
                      % (len(fresh), "、".join(fresh)))
        except Exception as exc:  # noqa: BLE001
            print("  （工具台账没更新成：%s；不影响收工）" % exc)

    # 0.75) 只读采样（G-04，2026-11-18 接入）：配置说该跑的 vs 机器上真在跑的对不对得上。
    #      放在所有写盘动作之前——报 F 时账本、视图、退路点一个字节都不动。
    print("\n=== 采样（只读，G-04）===")
    s_tier, s_lines, s_payload = run_sample()
    for x in s_lines:
        print(x)
    if s_payload:
        record_sample(s_payload, s_tier)
    if s_tier == STEP_HARD:
        return 1
    if s_tier == STEP_UNKNOWN:
        return 3
    if s_tier == STEP_WARN:
        print("  （这几项验不了，不拦记账；但也别当「已验过」）")

    # 0.79) 记忆快照刷新（G-12 B 步，2026-11-18 接入）：她检索每次现读文件，
    #      所以刷一遍导出就等于让她读到当前。只提醒不拦——导出失败不该卡住收工。
    print("\n=== 记忆快照刷新（G-12）===")
    print(run_memory_export())

    # 0.78) 坑候选计数（内部守则（略），2026-11-18 接入）：到 3 次还没进坑库的提醒一下。
    #      只提醒不拦——计数表不是「事实」，是「我有没有在记」的信号；拦收工不合适。
    print("\n=== 坑候选计数（内部守则（略））===")
    print(run_candidates())

    # 0.77) 配置漂移（G-04 五行，2026-11-18 接入）：文件里写的 vs 程序实际跑的。
    #       同样是只读，同样排在所有写盘动作之前。分档跟采样器一套：
    #       F 拦收工；U（缺对照表／缺会话／有差异但没人声明该是什么）只记，不拦。
    #       这里看的是它自己的 JSON 结论，不看退出码——退出码只用来交叉核对。
    print("\n=== 配置漂移（只读，G-04 五行）===")
    d_tier, d_lines = run_drift()
    for x in d_lines:
        print(x)
    if d_tier == STEP_HARD:
        return 1
    if d_tier == STEP_WARN:
        print("  （这几项判不了，不拦记账；别当「已验过」）")

    # 0.8) 派生视图：所有只读预检（摘要/守则/清单）都过了，才刷新四份自动视图。
    #      理由：索引/主题索引/技术卡/清单-完整 各有生成命令，只跑一条会让另外三份滞后；
    #      但它们会写盘，必须排在预检之后——预检失败时一个派生字节都不该动。
    #      失败不阻塞收工——视图滞后是警告级，账本丢了才是硬错。
    if "--no-refresh" not in sys.argv and os.path.exists(REFRESH):
        rr = subprocess.run([PY, REFRESH], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        print("\n=== 派生视图 ===")
        print((rr.stdout or "").strip())
        if classify_step(rr.returncode) != STEP_OK:
            if classify_step(rr.returncode) == STEP_UNKNOWN:
                print("! 视图刷新退出码未知（%s）：不确定刷没刷成，按没刷算"
                      % rr.returncode)
            else:
                print("! 有视图没刷新成（不阻塞收工；下次跑 refresh_all.py 补）")

    # 1) 账本：必须走 ledger_lock（内部守则（略））
    tmpdir = tempfile.mkdtemp(prefix="closeout_")
    tmpf = os.path.join(tmpdir, "ledger-entry.txt")
    with io.open(tmpf, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(entry + "\n")
    cmd = [PY, LEDGER_LOCK, "--owner", "desktop_bot", "append",
           "--file", LEDGER, "--text-file", tmpf]
    if os.path.exists(LEDGER):
        cmd += ["--expect-bytes", str(os.path.getsize(LEDGER))]
    lr = subprocess.run(cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    print((lr.stdout or "").strip())
    if lr.returncode != 0:
        print(f"! 账本没写进去（退出码 {lr.returncode}）—— 先别继续，按上面的提示处理")
        return 2

    print("\n已落盘：账本（共享状态/交接卡已停用，不再生成）")

    # 2) 退路：收工先打点（内部守则（略））
    try:
        gr = subprocess.run([PY, GUARD, "finish", did[:60]],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=180)
        out = (gr.stdout or "").strip()
        if out:
            print("\n=== 退路 ===")
            print(out)
        if classify_step(gr.returncode) == STEP_UNKNOWN:
            print("  （退路门退出码未知：%s——不确定退路打没打成，自检会再报）"
                  % gr.returncode)
        elif gr.returncode != 0:
            print("  （退路没打成；收工自检会报出来，先继续写账）")
    except Exception as exc:  # noqa: BLE001
        print(f"  （退路门没跑起来：{exc}）")

    # 3) 自检
    print("\n=== 自检 ===")
    r = subprocess.run([PY, CHECK], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print((r.stdout or "").strip())
    print(f"退出码 {r.returncode}")
    # 内部守则（略） 第 1 条：硬错必须拦。第 3 条（不留半成品）在这里做不到——自检要验「有没有漏
    # 记账」，只能跑在记账之后，账已写是既成事实；但退出码必须说清「这一轮不算收工」。
    tier = classify_step(r.returncode)
    if tier == STEP_HARD:
        print("  ! 自检有硬错：按上面清单改完再收（账已写，但这一轮不算收工）")
        return 1
    if tier == STEP_UNKNOWN:
        print("  ! 自检退出码未知（%s）：它没有给出「能收工」的结论，这一轮不算收工。" % r.returncode)
        print("    先单独跑 python tools\\sync_check.py，看它是被杀、超时，还是中途崩了。")
        return 3


    print("\n=== 收工三步（自己动手，本脚本不代劳）===")
    print("1. 干完的条目：从 临时本子-桌面助手.md 删掉")
    print("2. 要搁置的：挪进 待处理事项.md（本子上不留副本）")
    print("3. 说“成了”的：挂证据（verify.py）")

    print("\n=== 贴到新会话这段 ===")
    print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
