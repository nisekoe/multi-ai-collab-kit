"""同步自检：防止文档被埋、防止改了东西没落账、防止索引和现实脱节、
防止账本时间戳写到了未来。

用法：
    python sync_check.py            # 跑一遍，打印报告
退出码：0 全绿；1 有硬错（必须收工前修）；2 只有警告。

2026-11-16 起含「账本时间戳守门」（从 state_guard.py 并入，见下方注释）；
收工的唯一入口是 closeout.py，它会自动跑这个自检。
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import archive_paths as ap

# 控制台默认 GBK，打印 ✗ 这类字符会 UnicodeEncodeError 直接崩；统一转 UTF-8 输出。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

def _wb(name):
    return str(ap.resolve("工作台/" + name, for_write=False))
if ap.write_mode() == "hub":
    WB = str(ap.paths.resolve_root(for_write=False)[0])
else:
    WB = os.path.join(str(ap.old_root()), "工作台")
if ap.write_mode() == "hub":
    ARCH = os.path.join(str(ap.paths.resolve_root(for_write=False)[0]), "90-archive")
else:
    ARCH = os.path.join(str(ap.old_root()), "档案库")
LEDGER = _wb("工作台结果.md")
SCRATCH = _wb("临时本子-桌面助手.md")
NING_SCRATCH = _wb("临时本子-飞书助手.md")
PENDING = _wb("待处理事项.md")
GUARD_REPO = str(ap.old_root()) if ap.write_mode() == "old" else str(ap.paths.resolve_root(for_write=True)[0])
GUARD_WORKLOG = str(ap.resolve("工作台/工作框架/.guard/worklog.jsonl", for_write=True))
GIT_EXE = r"C:\Program Files\Git\cmd\git.exe"
PY_EXE = sys.executable
# PERSONA_CHECK 已于 2026-11-18 退休：它比的是 AstrBot data_v4.db personas.飞书助手，而 AstrBot 11-17 停用、
# 那个库没有任何活体在读。用户 11-18 20:5x 拍板「人设要不停成长和修正，该搬的都搬过来了，
# 没必要再加这个人格守卫」——见 待处理事项 G-34（closed）。persona_check.py 仍留作手动只读核档。
# 人格正本同步链路（由用户按各自工作台配置维护，此处仅做检查示例）
# feishu_bot-identity-preset\{persona-chat.txt, cordis.patch.yml}，靠 AGENTS.md 那条约定保证跟手。
MEMORY_CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_verify.py")

REQUIRED = ["临时本子-桌面助手.md", "临时本子-飞书助手.md", "待处理事项.md", "工作台结果.md"]
# 临时本子结构参数（2026-11-16 起，替代原「共享状态 1600 字预算」）
SCRATCH_MAX_ITEMS = 20
SCRATCH_TYPES = ("待发", "断点", "暂记")
SCRATCH_RE = re.compile(r"^- \[(莫|宁)-(\d{3})｜(待发|断点|暂记)\] .*更新 ")  # noqa: E501

errors: list[str] = []
warns: list[str] = []


def read(p: str) -> str:
    try:
        return io.open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def check_required():
    for name in REQUIRED:
        if not os.path.exists(_wb(name)):
            errors.append(f"必读文件缺失：{name}")


def check_scratch():
    """临时本子结构（2026-11-16 起替代共享状态结构检查）。

    查四件：条目数 ≤20、编号唯一、类型只能是 待发/断点/暂记、每条带「更新」。
    飞书助手那本只报警告（她可能正写到一半），桌面助手那本报硬错。
    """
    for path, owner, hard in ((SCRATCH, "莫", True), (NING_SCRATCH, "宁", False)):
        if not os.path.exists(path):
            continue
        text = read(path)
        items = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("- [")]
        name = os.path.basename(path)
        sink = (lambda m: errors.append(m)) if hard else (lambda m: warns.append(m))
        if len(items) > SCRATCH_MAX_ITEMS:
            sink(f"{name} 有 {len(items)} 条 > {SCRATCH_MAX_ITEMS} 条上限（干完的删、搁置的挪进待处理事项）")
        seen = {}
        for ln in items:
            m = SCRATCH_RE.match(ln)
            if not m:
                sink(f"{name} 条目格式不对（要 `- [莫-001｜待发] … ｜ 更新 MM-DD HH:MM`）：{ln[:60]}")
                continue
            if m.group(1) != owner:
                sink(f"{name} 里出现另一侧的编号：{ln[:40]}")
            num = m.group(2)
            if num in seen:
                sink(f"{name} 编号重复：{owner}-{num}")
            seen[num] = True


def check_pending():
    """搁置库：文件在、且至少有一条 `- [G-NN]`（空库等于把东西丢了）。"""
    if not os.path.exists(PENDING):
        errors.append("待处理事项.md 不存在")
        return
    text = read(PENDING)
    if not re.search(r"^- \[G-\d{2}\]", text, flags=re.M):
        warns.append("待处理事项.md 里没有 G-xx 条目（要么真的清空了，要么内容写丢了——核一下）")


def last_ledger_time() -> datetime | None:
    text = read(LEDGER)
    stamps = re.findall(r"^\[(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\]", text, flags=re.M)
    if not stamps:
        return None
    mm, dd, hh, mi = stamps[-1]
    now = datetime.now()
    try:
        return datetime(now.year, int(mm), int(dd), int(hh), int(mi))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 账本时间戳守门（2026-11-16 从 state_guard.py 并入，架构整合第 2 步）
#
# 为什么并进来：原来只有 state_guard 查「账本行首时间戳写到了未来」，而 state_guard
# 不在 closeout 链上；sync_check 在链上却只管「改了东西没记账」。两件事都是
# 「账本与现实不一致」，却分住在两个工具里，收工只跑一个就有一半看不见。
# 并入后判据不动：超前 60 秒以上＝**硬错**（那三次事故的形状都是"把还没到的时刻写成已完成"）；
# 账本行时间与产物 mtime 差 5 分钟以上＝**只报不拦**（做完过一会儿才记账是正常的）。
# 注意：state_guard 的**共享状态结构检查**（小节齐全/顺序、QQ 行唯一、下一步编号连续、
# 内容过期）仍留在 state_guard，没有并进来——所以 state_guard **暂不能删**。
# ---------------------------------------------------------------------------
LEDGER_FUTURE_TOL = 60        # 秒：允许的时钟差
LEDGER_MTIME_TOL = 300        # 秒：账本时间与产物 mtime 的允许差
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_LEDGER_TS_HEAD = re.compile(r"^\[(\d{2})-(\d{2}) (\d{2}):(\d{2})\]")
_LEDGER_TS_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")
_PRODUCT_RE = re.compile(
    r"([\w\u4e00-\u9fff][\w\u4e00-\u9fff.\-\\/]*\.(?:md|py|json|jsonl|ps1|cmd|txt|html|db))")


def _ledger_stamp(line: str, now):
    """取这一行行首的时间戳。返回 (datetime, 形状) 或 (None, 原因)。"""
    m = _LEDGER_TS_HEAD.match(line)
    if m:
        mm, dd, hh, mi = (int(x) for x in m.groups())
        try:
            return datetime(now.year, mm, dd, hh, mi), "head"
        except ValueError:
            return None, "解析不了（月/日越界）"
    m = _LEDGER_TS_FULL.match(line)
    if m:
        y, mm, dd, hh, mi = (int(x) for x in m.groups())
        try:
            return datetime(y, mm, dd, hh, mi), "full"
        except ValueError:
            return None, "解析不了（月/日越界）"
    return None, "没有行首时间戳"


def _resolve_products(line: str, root: str = PROJECT_ROOT) -> list[str]:
    """从账本行里认出真实存在的产物文件（认不出就返回空，不猜）。"""
    out: list[str] = []
    seen = set()
    for raw in _PRODUCT_RE.findall(line):
        name = raw.replace("/", "\\").lstrip("\\")
        if ":" in name or name in seen:
            continue
        seen.add(name)
        for cand in (os.path.join(root, name),
                     os.path.join(root, "档案库", "工作台", name),
                     os.path.join(root, "tools", name)):
            if os.path.isfile(cand) and cand not in out:
                out.append(cand)
                break
    return out


def check_ledger_timestamps():
    """账本时间戳守门：超前＝硬错，与产物 mtime 差太大＝警告。

    2026-11-17 补（GPT 审计 §4.1 第 8 条的尾巴）：同一份产物常被后面几行账再次提到，
    改一次文件，前面每一行都会显得「早于 mtime」。所以只按**最后一次提到它的那一行**
    判——旧行不再因为"后来又被改过"而报噪音。
    """
    if not os.path.exists(LEDGER):
        return
    now = datetime.now()
    today = now.strftime("%m-%d")
    try:
        with io.open(LEDGER, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:  # noqa: BLE001
        errors.append(f"账本读不了：{exc}")
        return

    last_mention = {}
    for lineno, line in enumerate(lines, 1):
        for prod in _resolve_products(line)[:4]:
            last_mention[prod] = lineno

    for lineno, line in enumerate(lines, 1):
        ts, shape = _ledger_stamp(line, now)
        if ts is None:
            if shape.startswith("解析不了"):
                warns.append(f"账本第 {lineno} 行时间戳{shape}：{line.strip()[:40]}")
            continue
        ahead = (ts - now).total_seconds()
        if ahead > LEDGER_FUTURE_TOL:
            errors.append(
                f"账本第 {lineno} 行时间戳 {ts:%m-%d %H:%M} 超前系统时间 "
                f"{int(ahead)} 秒（现在 {now:%m-%d %H:%M}）——"
                f"这是\"写了个还没到的时刻\"，别拿它当已完成的证据")
        if ts.strftime("%m-%d") != today:
            continue
        for prod in _resolve_products(line)[:4]:
            if last_mention.get(prod) != lineno:
                continue
            try:
                mt = os.path.getmtime(prod)
            except OSError:
                continue
            gap = ts.timestamp() - mt
            if abs(gap) <= LEDGER_MTIME_TOL:
                continue
            word = "晚" if gap > 0 else "早"
            warns.append(
                f"账本第 {lineno} 行写 {ts:%H:%M}，但 {os.path.basename(prod)} 的 mtime 是 "
                f"{datetime.fromtimestamp(mt):%H:%M}，{word} {abs(int(gap)) // 60} 分钟"
                f"（补记正常；但若写的是\"动作时间\"就该等于 mtime）")


def check_unledgered_changes(cutoff_minutes: int = 30):
    """工作台/档案库里比账本最后一条还新的 md，说明改了东西没落账。"""
    last = last_ledger_time()
    if last is None:
        warns.append("账本里没找到 [MM-DD HH:MM] 格式的条目")
        return
    recent = []
    SKIP_DIR_SUB = ("会话归档", "历史", "__pycache__")
    for root in (WB, ARCH):
        for d, _, files in os.walk(root):
            if any(k in d for k in SKIP_DIR_SUB):
                continue
            for f in files:
                if not f.endswith(".md"):
                    continue
                p = os.path.join(d, f)
                mt = datetime.fromtimestamp(os.path.getmtime(p))
                if mt > last and (datetime.now() - mt).total_seconds() > cutoff_minutes * 60:
                    recent.append((os.path.relpath(p, WB), mt.strftime("%m-%d %H:%M")))
    if recent:
        for name, mt in recent:
            warns.append(f"比账本新但没记账：{name}（{mt}，账本最后 {last:%m-%d %H:%M}）")


def check_index_registration():
    idx = read(INDEX)
    if not idx:
        errors.append("目录.md 不存在或为空")
        return
    # 目录里登记的 md 文件名
    listed = set(re.findall(r"`([^`]+\.md)`", idx))
    listed = {os.path.basename(x) for x in listed}
    # 工作台里实际存在的 md（不含子目录）
    actual = {f for f in os.listdir(WB) if f.endswith(".md")}
    missing = actual - listed - {"目录.md"}
    if missing:
        for m in sorted(missing):
            warns.append(f"工作台里有但目录没登记：{m}")
    # 目录里写了但文件不存在
    # 目录里写了但文件不存在（递归找，因为有些在 任务卡-详情/ 子目录）
    def find_any(name: str) -> bool:
        for root in (WB, ARCH):
            for d, _, files in os.walk(root):
                if name in files:
                    return True
        return False

    for name in sorted(listed):
        if "*" in name or "YYYY" in name:
            continue  # 通配/示例，不是真路径
        if not find_any(name):
            warns.append(f"目录登记的文件不存在：{name}")


TASK_MARK = re.compile(r"【任务】(.+?)【责任方】(.+?)【截止】(.*?)\s*$", re.M)


# ⚠ 2026-11-16 停用：任务卡.md 随交接体系整合作废（改由 临时本子 + 直连）。
# 函数保留只为可回溯；不要再把它接回 main()。
def check_task_card():
    text = read(CARD)
    if not text:
        return
    for ln in text.split("\n"):
        if "【任务】" in ln:
            if not TASK_MARK.search(ln) or "【责任方】" not in ln or "【截止】" not in ln:
                errors.append(f"任务卡有条目格式不对（必须一行写全三要素）：{ln[:50]}…")


def check_dead_paths_in_docs():
    """文档里引用的 工作台\\xxx.md 是否存在（只查 md 引用，避免噪音）。

    2026-11-11 20:50 修：**只查"活文档"，跳过账本/事件稿/归档/历史**。
    原因：账本和事件稿里大量出现"当时某文件在哪个路径"的历史记录，
    那些是**记录**不是**引用**；把它们当死链查，会产生永远消不掉的假阳性
    （实测：账本正文里写 `档案库\\工作台\\工作台结果.md`，被拼成
    `工作台\\工作台结果.md` 后误报"不存在"）。
    """
    # 2026-11-17：把中文标点从"路径字符"里剔出去。原来一行里写了几个
    # 「工作台\a.md，工作台\档案库\b.md」，逗号被当成路径的一部分，整串算一个
    # 假目标 → 报「钩子成型方案.md，工作台\档案库\… 不存在」。这是解析错，不是死链。
    pat = re.compile(r"工作台[\\/]([^\\s`'\"()（），。、；]+\\.md)")
    # 这些文件里的路径是"历史记录"，不是"活引用"，不参与死链检查
    SKIP_FILES = {"工作台结果.md", "踩坑清单.md", "变更流水.md", "工具清单.md"}
    SKIP_DIR_PARTS = ("历史", "事件库", "事件账簿", "坑库", "__pycache__")
    seen = set()
    for d, _, files in os.walk(WB):
        if any(k in d for k in SKIP_DIR_PARTS):
            continue
        for f in files:
            if not f.endswith(".md") or f in SKIP_FILES:
                continue
            p = os.path.join(d, f)
            for m in pat.finditer(read(p)):
                target = m.group(1).strip()
                if "*" in target:
                    continue  # 通配引用，跳过
                if target in seen:
                    continue
                seen.add(target)
                if target.startswith("档案库") or "档案库" in target:
                    continue  # 「工作台\档案库\x」是写错前缀的老写法，不是真引用
                if not os.path.exists(os.path.join(WB, target)):
                    # 历史记录里的引用不改：文件若已归档进 历史\，也算找得到
                    if os.path.exists(os.path.join(WB, "历史", target)):
                        continue
                    warns.append(f"{os.path.basename(p)} 引用了不存在的文件：工作台\\{target}")


def check_big_files():
    for root in (WB, ARCH):
        for d, _, files in os.walk(root):
            if "会话归档" in d:
                continue  # 会话全文本来就是大归档，不参与切片提醒
            for f in files:
                if not f.endswith(".md"):
                    continue
                p = os.path.join(d, f)
                kb = os.path.getsize(p) / 1024
                if kb > 60 and os.path.basename(p) not in ("收件箱.md", "回信.md", "待整理词条.md"):
                    warns.append(f"大文件 {kb:.0f}KB（考虑切片）：{os.path.relpath(p, WB)}")


def check_guard():
    """内部守则（略） 事后审计：这轮有没有退路。只报警告，不拦收工（拦了会逼人跳过自检）。"""
    if not os.path.isdir(os.path.join(GUARD_REPO, ".git")):
        return
    # 1) 有未提交改动 = 这一轮没走 finish
    try:
        r = subprocess.run([GIT_EXE, "-C", GUARD_REPO, "status", "--porcelain"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        left = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        if left:
            warns.append("退路：还有 %d 个未提交改动（收工前跑 python tools\\guard.py finish \"本轮结果\"）" % len(left))
    except Exception:  # noqa: BLE001
        pass
    # 2) 最近一轮改了文件却没有终点
    try:
        with io.open(GUARD_WORKLOG, encoding="utf-8", errors="replace") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        warns.append("退路账本还没建（guard.py 一次都没跑过）")
        return
    except json.JSONDecodeError:
        warns.append("退路账本有坏行（jsonl 解析失败）")
        return
    if not rows:
        warns.append("退路账本还是空的（guard.py 一次都没跑过）")
        return
    rounds = [r for r in rows if r.get("phase", "finish") == "finish"]
    if rounds:
        last = rounds[-1]
        if last.get("files_changed", 0) > 0 and not last.get("end_commit"):
            warns.append("退路：上一轮改了 %d 个文件但没留下退路点（%s）"
                         % (last.get("files_changed"), last.get("at")))
    # 开了工没收尾：start 了没有对应的 finish；这种轮次在"有没有未提交改动"里看不见。
    # 只报"最后一次 finish 之后"还挂着的（更早的已被后续提交收拢，没安全洞）。
    finished = {r.get("start_commit") for r in rounds if r.get("start_commit")}
    last_finish_at = max((r.get("at") or "" for r in rounds), default="")
    orphans = [r for r in rows if r.get("phase") == "start"
               and r.get("start_commit") not in finished
               and (r.get("at") or "") > last_finish_at]
    if orphans:
        warns.append("退路：有 %d 轮开了工没收尾（start 了没 finish），最近一轮 %s"
                     % (len(orphans), orphans[-1].get("at")))


def _run_readonly_check(path: str, label: str) -> None:
    """跑一个只读守卫工具，非 0 就记硬错（P3 人格 / P1 记忆）。"""
    try:
        r = subprocess.run([PY_EXE, path, "--quiet"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except Exception as exc:  # noqa: BLE001
        warns.append(f"{label} 没跑起来：{exc}")
        return
    out = (r.stdout or "").strip()
    if r.returncode == 0:
        return
    if r.returncode == 2:      # 0 全绿／2 有警告／1 硬错：别把警告报成硬错，也别吞掉
        warns.append(f"{label} 有警告（exit 2）：{out[:200]}")
        return
    errors.append(f"{label} exit {r.returncode}：{out[:200]}")


# ---------------------------------------------------------------------------
# 飞书飞书助手的默认值守卫（2026-11-16 立：用户要求新 IM 插件给飞书助手默认完全权限 + 插入式消息）
#
# 为什么必须盯：dsh-im 的 normalizeBusyMessageMode 只认字符串 "steer"，**缺键就落回 "queue"**，
# 而重建 bot / 插件重写 workspaces.json 都会让键消失，没人会发现。
# 权限则没有 per-bot 档，完全继承 profile 三行默认（sandbox-policy / approval / permission.defaultPreset），
# 少一行新会话就掉回 workspace-write。
# ---------------------------------------------------------------------------
DSH_HOME = os.path.join(os.path.expanduser("~"), ".dsh")
IM_FEISHU = os.path.join(DSH_HOME, "integrations", "dsh-feishu")
IM_CONFIG = os.path.join(IM_FEISHU, "config.json")
IM_WORKSPACES = os.path.join(IM_FEISHU, "workspaces.json")
PROFILE_PATCH = os.path.join(DSH_HOME, "profiles", "desktop", "cordis.patch.yml")
EXPECT_WORKSPACE = os.path.join(os.path.expanduser("~"), "Documents", "deepseek-harness", "default-workspace")
EXPECT_PRESET = "feishu_bot"
EXPECT_BUSY = "steer"


def _patch_block(text: str, row_id: str) -> str:
    """从 patch 文本里取出顶格 `- id: <row_id>` 那一段（到下一个顶格 `- ` 或文件尾）。"""
    m = re.search(r"^- id: " + re.escape(row_id) + r"\s*$", text, flags=re.M)
    if not m:
        return ""
    nxt = re.search(r"^- \S", text[m.end():], flags=re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[m.start():end]


def check_im_bot_defaults():
    """飞书助手那台飞书 bot 的四项默认：工作区 / 预设 / 插入式 / 完全权限。掉档＝硬错。"""
    try:
        cfg = json.loads(read(IM_CONFIG))
    except ValueError:
        cfg = {}
    bots = cfg.get("bots") or []
    bot_id = None
    for b in bots:
        if isinstance(b, dict) and b.get("botName") == "飞书助手":
            bot_id = b.get("id")
            break
    if bot_id is None and bots:
        bot_id = bots[0].get("id")
    if bot_id is None:
        warns.append("dsh-im 里没找到飞书 bot（跳过飞书助手默认值检查）")
    else:
        try:
            ws = json.loads(read(IM_WORKSPACES))
        except ValueError:
            ws = None
            errors.append("dsh-im workspaces.json 读不出来（飞书助手的插入式与预设无法确认）")
        if isinstance(ws, dict):
            busy = (ws.get("busyMessageModes") or {}).get(bot_id)
            if busy != EXPECT_BUSY:
                errors.append(
                    "飞书助手的消息处理方式不是插入式：busyMessageModes=%r（应为 'steer'；"
                    "**这个键缺省即 queue**，去设置页重选一次）" % (busy,))
            preset = (ws.get("agentPresets") or {}).get(bot_id)
            if preset != EXPECT_PRESET:
                errors.append("飞书助手的 agent preset 不是 %s（现在 %r）" % (EXPECT_PRESET, preset))
            wsp = (ws.get("workspaces") or {}).get(bot_id)
            if wsp != EXPECT_WORKSPACE:
                errors.append("飞书助手的工作区不是 %s（现在 %r）" % (EXPECT_WORKSPACE, wsp))

    text = read(PROFILE_PATCH)
    if not text:
        errors.append("读不到 profile 的 cordis.patch.yml（飞书助手的完全权限无法确认）")
        return
    for row, needle, label in (
        ("sandbox-policy", "mode: danger-full-access", "沙箱默认模式"),
        ("approval", "policy: never", "审批默认策略"),
        ("permission", "defaultPreset: danger-full-access", "权限默认档"),
    ):
        blk = _patch_block(text, row)
        if not blk:
            errors.append("profile 缺 %s 这一段（%s没了，新会话会掉回 workspace-write）" % (row, label))
        elif needle not in blk:
            errors.append("profile 的 %s 里没有 `%s`（%s掉档）" % (row, needle, label))


def check_persona_and_memory():
    """P1 记忆可信度：收工自检里跑这个只读工具。

    P3 人格守卫（persona_check）2026-11-18 起不再跑——它拿活正本去比已停用的 AstrBot 库，
    每轮收工都报硬错却拦不住任何真事故。用户拍板撤掉（G-34）。要手动核两边历史差异，
    直接跑 python tools\\persona_check.py，它只读、不改数据。
    """
    _run_readonly_check(MEMORY_CHECK, "记忆验收 memory_verify")


# ---------------------------------------------------------------- 环境噪音归类
# 2026-11-17 第二批（第二-3）：一次收工吐三十条警告，大半是「档案/索引本来就大」
# 「历史稿引用了已封存的旧文件名」「补记时差」——这些今天改不动，也不是问题。
# 狼来了喊久了，真警告会被跳过。这里只归类、不吞掉：--verbose 能看明细。
ARCHIVE_PARTS = ("..\\档案库", "飞书助手记忆正本", "封存", "历史", "会话归档")
LEDGER_TOL_MIN = 60   # 补记时差容忍（分钟）；超了才是「改了没记账」的嫌疑
_RETIRED_EXTRA = {"待整理词条.md", "双方守则.md", "钩子成型方案.md", "QQ.md", "DSH.md",
                  "重启中20261113.md"}
_retired_cache = None


def retired_names() -> set:
    """已封存/已作废的旧文件名（封存目录里有的全算，另加直接删掉的几个）。"""
    global _retired_cache
    if _retired_cache is None:
        names = set(_RETIRED_EXTRA)
        for d, _, files in os.walk(os.path.join(ARCH, "封存")):
            names.update(f for f in files if f.endswith(".md"))
        _retired_cache = names
    return _retired_cache


def tidy_warnings(warns):
    """把「设计如此／历史如此」的警告合并计数，剩下的才是要看的。

    判据只有一句：**这条警告我今天能不能改？** 改不动的归环境类。
    """
    keep, env = [], {}

    def bump(key):
        env[key] = env.get(key, 0) + 1

    for w in warns:
        if w.startswith("大文件 ") and any(k in w for k in ARCHIVE_PARTS):
            bump("档案/索引偏大（设计如此，不切片）")
        elif "引用了不存在的文件：" in w and \
                w.rsplit("工作台\\", 1)[-1].strip() in retired_names():
            bump("旧体系文件引用（目标已封存／已作废）")
        elif re.match(r"账本第 \d+ 行写 ", w) and "早 " in w:
            # 「早」= 账本记的时刻早于文件现在的 mtime，也就是这份文件后来又改过一次。
            # 文件本来就会反复改，git 检出也会刷 mtime —— 这条结构上就是噪音，不分分钟数。
            bump("账本行早于产物 mtime（文件后来又被改过，正常）")
        else:
            keep.append(w)
    return keep, env


def main() -> int:
    check_required()
    check_scratch()
    check_pending()
    # check_task_card / check_index_registration 已停用：
    # 任务卡.md、目录.md 2026-11-16 随交接体系整合作废（封存在 档案库\封存\20261116-旧交接体系\）。
    check_ledger_timestamps()
    check_unledgered_changes()
    check_dead_paths_in_docs()
    check_big_files()
    check_guard()
    check_im_bot_defaults()
    check_persona_and_memory()

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"=== 同步自检 {now} ===")
    if errors:
        print(f"\n【硬错 {len(errors)} 条】")
        for e in errors:
            print("  ✗", e)
    else:
        print("\n硬错：无")
    verbose = "--verbose" in sys.argv
    keep, env = tidy_warnings(warns)
    if keep:
        print(f"\n【警告 {len(keep)} 条】")
        for w in keep:
            print("  !", w)
    else:
        print("警告：无")
    if env:
        print(f"\n【已归类环境噪音 {sum(env.values())} 条】"
              "（改不动、也不是问题；--verbose 看明细）")
        for k, v in sorted(env.items(), key=lambda kv: -kv[1]):
            print(f"  · {k}：{v} 条")
    if verbose:
        print("\n【全部警告·明细】")
        for w in warns:
            print("  ~", w)

    if errors:
        return 1
    # 退出码跟着**显示出来的**走：已归类成环境噪音的不算警告，
    # 否则屏幕上写着「警告：无」、退出码却是 2，外面按退出码读就会误判。
    return 2 if keep else 0


if __name__ == "__main__":
    sys.exit(main())