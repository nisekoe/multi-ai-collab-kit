# -*- coding: utf-8 -*-
r"""safe_write：写文本文件的**唯一入口**（P0-2，2026-11-12）。

为什么要有这个文件（根因一句话）
    **写文件没有唯一入口。** Python 有 Python 的默认编码、PowerShell 5.1 有它自己的、
    `>` 和 `Set-Content` 还不一样 —— 每多一个入口，就多一种坏法。
    这台机器上真炸过的：`.ps1` 一改整页语法崩（PS 5.1 按 GBK 读无 BOM 的脚本）、
    `term` 变成 TAB+erm 与 `aliases` 变成 BEL+liases（PS 双引号里反引号+字母是转义）、
    文件变 UTF-16（`>`/`Out-File`）、中文变 `??`（老环境 `Set-Content` 是 ANSI）、
    CRLF/LF 混用（增量读按行长度推进会算错）。

它做五件事
    1. **统一编码**：UTF-8 **无 BOM** + **LF** 换行。例外：`.ps1/.psm1` **必须有 BOM**
       （Windows PowerShell 5.1 靠 BOM 才认 UTF-8，否则按 GBK 解析，含中文必崩）。
    2. **拒写控制字符**（除 `\t` `\n` `\r`）：命中就报行号、一个字节都不写。
       G1b 那种"反引号+字母"坏法肉眼看不见，只能在这里挡住。
    3. **写完必须能解析**（2026-11-12 14:2x 加）：`.json` 要 `json.loads` 过、`.jsonl` 每行都要过、
       `.py` 要 `compile()` 过；`.json` 不许"追加"。**写出去的东西必须还能被读回来。**
    4. **原子落盘**：同目录临时文件 → fsync → `os.replace` 换上去。
       中途崩了只会留旧版本，不会留半截新文件。
    5. **写完自读比对**：字节数 + sha1 与预期不符就算失败（并回滚到备份）。
    6. **体检模式**：`--check` 判一个已存在的文件有没有踩上面这些坑。
    7. **拒写 U+FFFD**（2026-11-13 立，飞书助手当晚踩到）：中文字经 PowerShell 管道/错误代码页
       会坏成替换符，写进盘就固化了。`--check` 早能查出，但**写的时候没人拦**——
       现在是硬闸（退出码 7），只有重写已知历史残档才加 `--allow-replacement`。

用法
    # 落盘（推荐 --text-file，正文长、中文多时别用命令行传）
    python tools\safe_write.py --file 某文件.md --text-file 正文.txt
    python tools\safe_write.py --file 某脚本.ps1 --text-file 正文.txt --backup

    # 只看规则怎么判、不落盘
    python tools\safe_write.py --file 某文件.md --text-file 正文.txt --dry-run

    # 追加（账本 / jsonl / 日志——**不许整篇重写**）
    python tools\safe_write.py --file 某.jsonl --text-file 行.txt --append

    # 体检一个已有文件（不写）
    python tools\safe_write.py --check 某文件.ps1

    # 在 Python 里用（别的脚本产出 .md/.json/.jsonl/.txt 都该走这里）
    from safe_write import write_text
    write_text(path, text, backup=True)

退出码
    0 成功 / 体检干净
    1 体检发现违规（--check 模式）
    2 参数或 IO 问题（文件不存在、目录不可写、目标不是文本扩展名）
    3 拒写：正文含控制字符（G1b 那一类）
    4 写完自读比对不一致（已回滚到写入前的备份）
    5 编码规则冲突（例如要求把 .ps1 写成无 BOM）
    6 拒写：内容解析不了（.json 非法转义/尾逗号、.jsonl 某行坏、.py 语法错；
      或试图"追加"到 .json）
    7 拒写：正文含 U+FFFD 替换符（中文字在链路上损坏、原始字节已丢）；
       只重写已知历史残档时用 --allow-replacement 放行

依据
    搜了业界做法：
      - 原子写 = 同目录临时文件 + fsync + `os.replace`（durablewrite / fs-transaction /
        GPG-Meister 的 atomic_write 都是这个序列；Windows 上 `os.replace` 仍原子，
        目录 fsync 是空操作）。
        https://pypi.org/project/durablewrite/0.2.0/ 、 https://pypi.org/project/fs-transaction/
      - CRLF→LF 归一化：microsoft/apm 的 `write_text_lf`（"CRLF 输入写成 LF，磁盘字节与平台无关"）
        https://github.com/microsoft/apm/commit/ad52ad5dd98d7af95bb52bb49fb113690c797d34
      - UTF-8 不该带 BOM：Unicode 邮件列表的通行意见 + 本机实测（.ps1 是唯一例外）
        http://unicode.org/mail-arch/unicode-ml/y2002-m02/0363.html
    自拟（未经外部验证）：控制字符闸、按扩展名分流的 BOM 策略、写完自读比对、
      以及"体检模式"的具体判词。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

# .ps1/.psm1 在 Windows PowerShell 5.1 下**必须**有 BOM
BOM_REQUIRED_EXTS = {".ps1", ".psm1"}
# 允许本工具写的文本扩展名（防止被拿去写二进制）
TEXT_EXTS = {
    ".md", ".py", ".json", ".jsonl", ".yaml", ".yml", ".ps1", ".psm1", ".cmd", ".bat",
    ".txt", ".log", ".ini", ".cfg", ".toml", ".html", ".css", ".js", ".ts", ".csv", ".xml",
}
# 控制字符：与 encoding_check 的控制字符硬错同一区间（\t \n \r 不算）
CONTROL_RANGES = ((0x00, 0x08), (0x0B, 0x0C), (0x0E, 0x1F))
BOM_BYTES = b"\xef\xbb\xbf"


class SafeWriteError(Exception):
    """带退出码的失败。code 直接当进程退出码用。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- 规则检查

def _is_control(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in CONTROL_RANGES)


def find_control_chars(text: str) -> list[tuple[int, int, int]]:
    """返回 [(行号, 列号, 码点)]，行号/列号从 1 开始。"""
    hits: list[tuple[int, int, int]] = []
    line = 1
    col = 0
    for ch in text:
        if ch == "\n":
            line += 1
            col = 0
            continue
        col += 1
        if _is_control(ch):
            hits.append((line, col, ord(ch)))
    return hits


def normalize_newlines(text: str) -> tuple[str, int]:
    """CRLF/CR → LF。返回 (新文本, 被改掉的换行数)。"""
    n_crlf = text.count("\r\n")
    n_cr = text.count("\r") - n_crlf
    if not (n_crlf or n_cr):
        return text, 0
    return text.replace("\r\n", "\n").replace("\r", "\n"), n_crlf + n_cr


def decide_bom(path: str, force_bom=None) -> bool:
    """该不该写 BOM。.ps1/.psm1 必须有；其余默认没有。"""
    ext = os.path.splitext(path)[1].lower()
    required = ext in BOM_REQUIRED_EXTS
    if force_bom is None:
        return required
    if required and not force_bom:
        raise SafeWriteError(
            5,
            "%s 必须以带 BOM 的 UTF-8 落盘 —— Windows PowerShell 5.1 读到无 BOM 的脚本会按 GBK 解析，"
            "含中文就整页语法崩（G1）。这条不许用 --no-bom 关掉。" % ext)
    return bool(force_bom)


def _validate_content(path: str, text: str) -> None:
    """写完必须能"读回来解析得动"——防止写出语法坏掉的 .json/.jsonl/.py。

    2026-11-12 14:2x 加（用户："JSON 的编写也在符号上翻车了，能不能一并纳入修复"）。
    事实：我往 强制入口.json 里写了一个 Windows 路径 `plugin_demo\\wake_context.py`，
    反斜杠 `\\w` 在 JSON 里是**非法转义** → 整个文件读不了。发现它纯属运气（刚好 rules_check 会 json.load）。
    **能发现是运气，就不能算防住了。** 所以把"能不能解析"变成写盘前的硬闸：
      - .json  → json.loads 必须成功
      - .jsonl → 每个非空行都必须能 json.loads
      - .py    → compile() 必须成功（等价于 py_compile 的语法检查）
    与 G1b（控制字符）是同一族：**写出去的东西必须还能被读回来。**
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".json", ".jsonl", ".py"):
        return

    if ext == ".json":
        try:
            json.loads(text)
        except ValueError as exc:
            raise SafeWriteError(
                6,
                "拒写：%s 不是合法 JSON —— %s\n"
                "      常见原因：Windows 路径里的反斜杠没写成 \\\\（`\\w` `\\a` `\\t` 在 JSON 里是非法/意外转义），"
                "或尾逗号、缺引号。写成 / 或双反斜杠再试。" % (os.path.basename(path), exc))
        return

    if ext == ".jsonl":
        for i, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except ValueError as exc:
                raise SafeWriteError(
                    6, "拒写：%s 第 %d 行不是合法 JSON —— %s"
                       % (os.path.basename(path), i, exc))
        return

    # .py：语法必须过（不执行，只编译）
    try:
        compile(text, path, "exec")
    except SyntaxError as exc:
        raise SafeWriteError(
            6, "拒写：%s 第 %s 行语法错 —— %s"
               % (os.path.basename(path), exc.lineno, exc.msg))


def find_replacement_chars(text: str) -> list[tuple[int, int, str]]:
    """U+FFFD 替换符的位置：行号、该行个数、行首 30 字（定位用）。

    U+FFFD 出现说明**原文已经在上游损坏、原始字节已丢**，能修回来的只有重跑源头。
    """
    return [
        (i + 1, line.count("\ufffd"), line.strip()[:30])
        for i, line in enumerate(text.split("\n"))
        if "\ufffd" in line
    ]


def guard_replacement(text: str, allow: bool) -> None:
    """正文含替换符就拒写（2026-11-13 立：中文字经工具链传递会坏成 U+FFFD）。

    为什么必须挡：坏掉的字节写进盘就固化了，之后谁也分不清"原文如此"还是"路上坏了"。
    """
    if allow:
        return
    reps = find_replacement_chars(text)
    if not reps:
        return
    total = sum(n for _l, n, _s in reps)
    raise SafeWriteError(
        7,
        "拒写：正文含 %d 个 U+FFFD 替换符（%d 行；首处第 %d 行：%s…）。\n"
        "      U+FFFD 表示原文已在链路上损坏、原始字节已丢——写下去等于把坏字节固化。\n"
        "      先查来路（多半是中文经 PowerShell 管道/错误代码页解码）；\n"
        "      确认是有意保留历史残档，再加 --allow-replacement。"
        % (total, len(reps), reps[0][0], reps[0][2]),
    )


def build_bytes(path: str, text: str, *, force_bom=None, allow_crlf=False,
                allow_replacement=False) -> dict:
    """把正文变成要落盘的字节，同时跑完所有闸。不碰磁盘。"""
    ext = os.path.splitext(path)[1].lower()
    if ext and ext not in TEXT_EXTS:
        raise SafeWriteError(2, "不写非文本扩展名 %s（本工具只管文本文件）" % ext)

    warnings: list[str] = []
    if allow_crlf:
        out_text = text
    else:
        out_text, n_fixed = normalize_newlines(text)
        if n_fixed:
            warnings.append("已把 %d 处 CRLF/CR 归一成 LF" % n_fixed)

    hits = find_control_chars(out_text)
    if hits:
        lines = "、".join(str(l) for l, _c, _o in hits[:8])
        sample = " ".join("U+%04X@行%d" % (o, l) for l, _c, o in hits[:5])
        raise SafeWriteError(
            3,
            "拒写：正文含 %d 个控制字符（第 %s 行；%s）。\n"
            "      典型来路是 PowerShell 双引号里写了「反引号+字母」——"
            "`t 是 TAB、`a 是 BEL、`n 是换行（G1b）。\n"
            "      本工具不接受这种正文：请改用单引号 here-string（@'...'@），"
            "或把反引号换成普通字符。" % (len(hits), lines, sample))

    guard_replacement(out_text, allow_replacement)

    bom = decide_bom(path, force_bom)
    # 写完必须能解析（.json/.jsonl/.py）——见 _validate_content 的说明
    _validate_content(path, out_text)
    body = out_text.encode("utf-8")
    data = (BOM_BYTES if bom else b"") + body
    return {
        "data": data,
        "text": out_text,
        "bom": bom,
        "warnings": warnings,
        "bytes": len(data),
        "sha1": hashlib.sha1(data).hexdigest(),
    }


def inspect(path: str) -> list[str]:
    """体检一个已存在的文件，返回违规列表（空 = 干净）。"""
    problems: list[str] = []
    ext = os.path.splitext(path)[1].lower()
    try:
        raw = open(path, "rb").read()
    except OSError as exc:
        raise SafeWriteError(2, "读不了：%s（%s）" % (path, exc))

    has_bom = raw.startswith(BOM_BYTES)
    if ext in BOM_REQUIRED_EXTS and not has_bom:
        try:
            text_try = raw.decode("utf-8")
            has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in text_try)
        except UnicodeDecodeError:
            has_cjk = True
        problems.append(
            "缺 BOM：%s %s —— PS 5.1 会按 GBK 读，含中文必崩" % (ext, "（含中文）" if has_cjk else "（纯 ASCII）"))

    try:
        text = raw[3:].decode("utf-8") if has_bom else raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        problems.append("不是合法 UTF-8：%s" % exc)
        return problems

    if has_bom and ext not in BOM_REQUIRED_EXTS:
        problems.append("多余 BOM：%s 不该带 BOM（json.loads/grep 会把 BOM 当正文）" % ext)

    crlf = text.count("\r\n")
    bare_cr = text.count("\r") - crlf
    if crlf or bare_cr:
        problems.append("换行不是纯 LF：CRLF %d 处、裸 CR %d 处" % (crlf, bare_cr))

    hits = find_control_chars(text)
    if hits:
        problems.append("控制字符 %d 处（第 %s 行；%s）"
                        % (len(hits),
                           "、".join(str(l) for l, _c, _o in hits[:8]),
                           " ".join("U+%04X" % o for _l, _c, o in hits[:5])))

    n_bad = text.count("\ufffd")
    if n_bad:
        problems.append("含已损坏的 U+FFFD %d 处（原始字节已丢）" % n_bad)
    return problems


# --------------------------------------------------------------------------- 落盘

def _backup(path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = "%s.bak-%s" % (path, stamp)
    shutil.copy2(path, bak)
    return bak


def append_text(path: str, text: str, *, allow_crlf: bool = False,
                allow_replacement: bool = False) -> dict:
    """**追加**（给账本、jsonl、日志用）。不整篇重写——账本重写正是 11-11 丢 112KB 的形状。

    与 write_text 共用同一套闸：归一换行、拒控制字符、按扩展名补 BOM（仅新建文件时）。
    追加写本身就是原子的（O_APPEND 单次 write），不需要临时文件替换。
    """
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    if ext and ext not in TEXT_EXTS:
        raise SafeWriteError(2, "不写非文本扩展名 %s（本工具只管文本文件）" % ext)
    if ext == ".json":
        # .json 是一个整体结构，追加必然产生非法 JSON —— 直接拦住（避免越写越坏）
        raise SafeWriteError(
            6, "拒写：.json 不能追加（追加后就不再是合法 JSON）。要改就整篇重写：去掉 --append。")

    warnings: list[str] = []
    if allow_crlf:
        out_text = text
    else:
        out_text, n_fixed = normalize_newlines(text)
        if n_fixed:
            warnings.append("已把 %d 处 CRLF/CR 归一成 LF" % n_fixed)

    hits = find_control_chars(out_text)
    if hits:
        lines = "、".join(str(l) for l, _c, _o in hits[:8])
        raise SafeWriteError(
            3, "拒写：追加内容含 %d 个控制字符（第 %s 行）——多半是 PowerShell 双引号里"
               "「反引号+字母」被当转义（G1b）。" % (len(hits), lines))

    guard_replacement(out_text, allow_replacement)

    # 追加的内容也要能解析：.jsonl 的每一行都得是合法 JSON
    _validate_content(path, out_text)

    is_new = not os.path.exists(path) or os.path.getsize(path) == 0
    prefix = b""
    if is_new:
        # 新建文件才谈 BOM：.ps1 要，其余不要（已有文件不动它的头）
        if decide_bom(path):
            prefix = BOM_BYTES
    data = prefix + out_text.encode("utf-8")
    with open(path, "ab") as fh:
        fh.write(data)

    with open(path, "rb") as fh:
        raw = fh.read()
    return {
        "bytes": len(data), "total_bytes": len(raw),
        "sha1": hashlib.sha1(data).hexdigest(),
        "bom": bool(prefix), "warnings": warnings, "appended": True,
    }


def _atomic_write(path: str, data: bytes) -> None:
    """同目录临时文件 → fsync → os.replace。中途崩了不会留半截。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".safe_write-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def write_text(path: str, text: str, *, force_bom=None, backup: bool = False,
               allow_crlf: bool = False, dry_run: bool = False,
               mkdir: bool = False, allow_replacement: bool = False) -> dict:
    """唯一入口：闸全过 → 落盘 → 自读比对。返回结果字典。"""
    path = os.path.abspath(path)
    plan = build_bytes(path, text, force_bom=force_bom, allow_crlf=allow_crlf,
                       allow_replacement=allow_replacement)

    if mkdir:
        os.makedirs(os.path.dirname(path), exist_ok=True)

    if dry_run:
        plan["dry_run"] = True
        plan["backup"] = None
        return plan

    bak = None
    if backup and os.path.exists(path):
        bak = _backup(path)

    _atomic_write(path, plan["data"])

    # 写完自读比对：字节数 + sha1（with关句柄，消ResourceWarning；2026-11-22）
    with open(path, "rb") as fh:
        raw = fh.read()
    got_bytes = len(raw)
    got_sha1 = hashlib.sha1(raw).hexdigest()
    if got_bytes != plan["bytes"] or got_sha1 != plan["sha1"]:
        detail = ("写后比对失败：期望 %d 字节 / sha1 %s，实际 %d 字节 / sha1 %s"
                  % (plan["bytes"], plan["sha1"][:12], got_bytes, got_sha1[:12]))
        if bak:
            shutil.copy2(bak, path)
            raise SafeWriteError(4, detail + "；已从备份回滚 %s" % os.path.basename(bak))
        raise SafeWriteError(4, detail + "；没有备份，没东西可回滚 —— 请手工核对")

    plan["backup"] = bak
    plan["verified"] = True
    return plan


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="写文本文件的唯一入口：UTF-8/LF/拒控制字符/原子/自读比对")
    ap.add_argument("--file", help="目标文件")
    ap.add_argument("--text-file", help="正文文件（UTF-8，可带 BOM）")
    ap.add_argument("--text", help="正文（短文本用；长文本别用命令行传）")
    ap.add_argument("--check", metavar="FILE", help="只体检，不写")
    ap.add_argument("--dry-run", action="store_true", help="只判规则，不落盘")
    ap.add_argument("--append", action="store_true",
                    help="追加而不是整篇重写（账本/jsonl/日志用这个）")
    ap.add_argument("--backup", action="store_true", help="写前备份到 .bak-时间戳")
    ap.add_argument("--allow-crlf", action="store_true", help="保留原换行（默认归一成 LF）")
    ap.add_argument("--allow-replacement", action="store_true",
                    help="允许正文含 U+FFFD 替换符（只用于重写已知的历史残档；默认拒写，退出码 7）")
    ap.add_argument("--mkdir", action="store_true", help="父目录不存在就建")
    ap.add_argument("--bom", dest="bom", action="store_true", default=None, help="强制写 BOM")
    ap.add_argument("--no-bom", dest="bom", action="store_false", help="强制不写 BOM")
    args = ap.parse_args(argv)

    try:
        if args.check:
            problems = inspect(args.check)
            print("=== safe_write 体检：%s" % args.check)
            if not problems:
                print("    干净：UTF-8 编码合规、LF 换行、无控制字符、BOM 符合扩展名要求。")
                return 0
            print("    发现 %d 项违规：" % len(problems))
            for p in problems:
                print("      -", p)
            return 1

        if not args.file:
            print("需要 --file（或用 --check 体检一个文件）")
            return 2
        if args.text_file:
            # newline="" 必须加：默认的通用换行模式会把 CRLF 悄悄转成 LF，
            # 那样"混用换行"这条闸就永远看不到 CRLF，等于白写。
            text = open(args.text_file, encoding="utf-8-sig", newline="").read()
        elif args.text is not None:
            text = args.text
        else:
            print("需要 --text-file 或 --text 提供正文")
            return 2

        if args.append:
            if args.dry_run:
                norm, n_fix = (text, 0) if args.allow_crlf else normalize_newlines(text)
                hits = find_control_chars(norm)
                if hits:
                    raise SafeWriteError(
                        3, "拒写：追加内容含 %d 个控制字符（第 %s 行）"
                           % (len(hits), "、".join(str(l) for l, _c, _o in hits[:8])))
                guard_replacement(norm, args.allow_replacement)
                print("=== safe_write --append（dry-run，未落盘）：%s" % args.file)
                print("    将追加 %d 字%s" % (len(norm), "，并归一 %d 处换行" % n_fix if n_fix else ""))
                return 0
            plan = append_text(args.file, text, allow_crlf=args.allow_crlf,
                                  allow_replacement=args.allow_replacement)
            plan["text"] = text
        else:
            plan = write_text(args.file, text, force_bom=args.bom, backup=args.backup,
                              allow_crlf=args.allow_crlf, dry_run=args.dry_run,
                              mkdir=args.mkdir, allow_replacement=args.allow_replacement)
        head = "（dry-run，未落盘）" if plan.get("dry_run") else ("已追加" if plan.get("appended") else "已写入")
        print("=== safe_write %s：%s" % (head, args.file))
        print("    %d 字节 / %d 字 / sha1 %s" % (plan["bytes"], len(plan["text"]), plan["sha1"][:12]))
        if plan.get("appended"):
            print("    追加后文件共 %d 字节" % plan["total_bytes"])
        print("    编码：UTF-8 %s / 换行：LF"
              % ("带 BOM（.ps1 必需）" if plan["bom"] else "无 BOM"))
        for w in plan["warnings"]:
            print("    提示：", w)
        if plan.get("backup"):
            print("    备份：", os.path.basename(plan["backup"]))
        if plan.get("verified"):
            print("    自读比对：一致（字节数 + sha1）")
        return 0
    except SafeWriteError as exc:
        print(exc.message)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
