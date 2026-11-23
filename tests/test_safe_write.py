
# -*- coding: utf-8 -*-
r"""safe_write 的反向用例（P0-2 验收）。

规矩：**没有反向用例的闸等于没有闸** —— 自己要能证明它会拦。
所以这里每条都要求"必须被拦住"，而不是"看起来写了检查"。

四条反向用例（P0-2 验收要求）
    ① 无 BOM 的 .ps1        → 必须拦住（写的时候拒；已有文件体检要报）
    ② 双引号拼反引号        → BEL/TAB 那类控制字符必须拦住
    ③ 控制字符              → 必须拦住，且报行号
    ④ CRLF/LF 混用          → 不许写出混用文件（归一成 LF）
外加：正常写文件 0 影响（.md 无 BOM / .ps1 自动带 BOM / 字节与 sha1 自读一致）。

跑法：
    python tools\test_safe_write.py
退出码：0 全过；1 有 FAIL。

注意：本文件自己**不含**真实控制字符（一律用 chr() 现造），
否则 encoding_check 会把它当 G1b 标本抓出来。
"""

from __future__ import annotations
import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

import importlib.util
import os
import subprocess
import sys
import tempfile

if __name__ == "__main__":

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    SPEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "safe_write.py")
    spec = importlib.util.spec_from_file_location("safe_write_under_test", SPEC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    TMP = tempfile.mkdtemp(prefix="safe_write_case_")
    fails: list[str] = []


    def check(name: str, cond: bool, detail="") -> None:
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(detail)))
        if not cond:
            fails.append(name)


    def run_cli(*argv) -> tuple[int, str]:
        py = sys.executable
        proc = subprocess.run([py, SPEC] + list(argv), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


    def wf(name: str, data: str | bytes) -> str:
        """造一个输入正文文件（按字节写，控制字符才不会被加工）。"""
        p = os.path.join(TMP, name)
        raw = data.encode("utf-8") if isinstance(data, str) else data
        with open(p, "wb") as fh:
            fh.write(raw)
        return p


    # ---------------------------------------------------------------- ① 无 BOM 的 .ps1
    ps1 = os.path.join(TMP, "case1.ps1")

    # 1a 想把它写成"无 BOM" → 必须被拒（exit 5）
    try:
        mod.write_text(ps1, "Write-Host '中文'\n", force_bom=False)
        check("①a 拒绝把 .ps1 写成无 BOM", False, "居然写成功了")
    except mod.SafeWriteError as exc:
        check("①a 拒绝把 .ps1 写成无 BOM", exc.code == 5 and "BOM" in exc.message, exc.message[:80])

    # 1b 正常路径：.ps1 自动带 BOM，且磁盘上真有那三个字节
    plan = mod.write_text(ps1, "Write-Host '中文'\n")
    raw = open(ps1, "rb").read()
    check("①b .ps1 自动带 BOM 且落盘可验", plan["bom"] and raw.startswith(b"\xef\xbb\xbf") and plan["verified"])

    # 1c 已有的"无 BOM 的 .ps1"（模拟手改丢 BOM）→ 体检必须报，且 CLI exit 1
    nobom = os.path.join(TMP, "case1c.ps1")
    with open(nobom, "wb") as fh:
        fh.write("Write-Host '中文'\n".encode("utf-8"))
    probs = mod.inspect(nobom)
    check("①c 体检抓到无 BOM 的 .ps1", any("缺 BOM" in p for p in probs), probs)
    rc, out = run_cli("--check", nobom)
    check("①c CLI 体检退出码 1", rc == 1, (rc, out[:120]))

    # ---------------------------------------------------------------- ② 双引号拼反引号
    # PS 里 `a = BEL(0x07)、`t = TAB(0x09)、`n = 换行。BEL 必须拒；TAB 是合法空白，放行。
    bel_text = "echo " + chr(7) + "liases\n"
    bel_in = wf("case2.txt", bel_text)
    target2 = os.path.join(TMP, "case2.md")
    rc, out = run_cli("--file", target2, "--text-file", bel_in)
    check("②a BEL（`a 转义来的）被拒收，exit 3", rc == 3, (rc, out[:160]))
    check("②b BEL 时一个字节都不写", not os.path.exists(target2))

    # TAB 允许（不然 \t 正常的缩进也被挡了）
    tab_plan = mod.write_text(os.path.join(TMP, "case2tab.md"), "a\tb\n")
    check("②c 正常 TAB 放行（不误伤）", tab_plan["verified"])

    # ---------------------------------------------------------------- ③ 控制字符
    ctrl_text = "第一行正常\n第二行有" + chr(0) + "空字节\n第三行有" + chr(0x0B) + "垂直制表\n"
    ctrl_in = wf("case3.txt", ctrl_text)
    target3 = os.path.join(TMP, "case3.md")
    rc, out = run_cli("--file", target3, "--text-file", ctrl_in)
    check("③a 控制字符被拒收，exit 3", rc == 3, (rc, out[:160]))
    check("③b 报出行号（第 2、3 行）", ("第 2、3 行" in out), out[:200])
    check("③c 控制字符时一个字节都不写", not os.path.exists(target3))

    # ---------------------------------------------------------------- ④ CRLF/LF 混用
    crlf_in = wf("case4.txt", "第一行\r\n第二行\n第三行\r\n")
    target4 = os.path.join(TMP, "case4.md")
    rc, out = run_cli("--file", target4, "--text-file", crlf_in)
    disk = open(target4, "rb").read()
    check("④a CRLF 输入落盘后是纯 LF", rc == 0 and b"\r" not in disk, (rc, disk[:40]))
    check("④b 混用被如实提示", "归一成 LF" in out, out[:200])
    probs = mod.inspect(target4)
    check("④c 落盘结果体检干净", probs == [], probs)

    # ---------------------------------------------------------------- ⑥ 写完必须能解析
    # 2026-11-12 14:2x（用户："JSON 的编写也在符号上翻车了，能不能一并纳入"）
    # 事实：我往 强制入口.json 写 Windows 路径时用了 `\w`，JSON 非法转义，整份读不了。

    # 6a .json 里的非法转义（正是我踩的那个）必须被拒
    bad_json = '{"a": "X:\\\\demo\\\\x", "b": "plugin_demo\\wake_context.py"}\n'
    bad_in = wf("case6a.txt", bad_json)
    target6 = os.path.join(TMP, "case6a.json")
    rc, out = run_cli("--file", target6, "--text-file", bad_in)
    check("6a JSON 非法转义（\\w）被拒，exit 6", rc == 6, (rc, out[:200]))
    check("6a 被拒时一个字节都不写", not os.path.exists(target6))

    # 6b 尾逗号也必须被拒
    comma_in = wf("case6b.txt", '{"a": 1,}\n')
    target6b = os.path.join(TMP, "case6b.json")
    rc, out = run_cli("--file", target6b, "--text-file", comma_in)
    check("6b JSON 尾逗号被拒，exit 6", rc == 6, (rc, out[:160]))

    # 6c 合法 JSON 必须放行（不误伤）
    good_in = wf("case6c.txt", '{"path": "X:/demo/x/y.py", "n": 2}\n')
    target6c = os.path.join(TMP, "case6c.json")
    rc, out = run_cli("--file", target6c, "--text-file", good_in)
    check("6c 合法 JSON 放行，exit 0", rc == 0, (rc, out[:160]))

    # 6d .jsonl 某行坏 → 必须报行号
    jl_in = wf("case6d.txt", '{"a":1}\n不是JSON\n')
    target6d = os.path.join(TMP, "case6d.jsonl")
    rc, out = run_cli("--file", target6d, "--text-file", jl_in)
    check("6d jsonl 坏行被拒，exit 6 且报第 2 行", rc == 6 and "第 2 行" in out, (rc, out[:200]))

    # 6e .py 语法错必须被拒
    py_in = wf("case6e.txt", "def f(:\n    pass\n")
    target6e = os.path.join(TMP, "case6e.py")
    rc, out = run_cli("--file", target6e, "--text-file", py_in)
    check("6e Python 语法错被拒，exit 6", rc == 6, (rc, out[:200]))

    # 6f .json 不许"追加"（追加后必然非法）
    jl_target = os.path.join(TMP, "case6f.json")
    rc, out = run_cli("--file", jl_target, "--text-file", good_in, "--append")
    check("6f .json 追加被拒，exit 6", rc == 6, (rc, out[:200]))

    # 6g .jsonl 追加合法行放行
    ok_line = wf("case6g.txt", '{"k": "v"}\n')
    rc, out = run_cli("--file", target6c.replace(".json", ".jsonl"), "--text-file", ok_line, "--append")
    check("6g jsonl 追加合法行放行，exit 0", rc == 0, (rc, out[:160]))

    # 6h 正文含 U+FFFD 替换符 → 必须拒写（2026-11-13 立，飞书助手踩到）
    fffd_in = wf("case6h.txt", "正常一行" + chr(0xFFFD) + "坏掉\n")
    target6h = os.path.join(TMP, "case6h.md")
    rc, out = run_cli("--file", target6h, "--text-file", fffd_in)
    check("6h U+FFFD 被拒，exit 7 且报行号", rc == 7 and "第 1 行" in out, (rc, out[:200]))
    check("6h 被拒时一个字节都不写", not os.path.exists(target6h))

    # 6i 重写已知历史残档：--allow-replacement 必须放行（不许误伤）
    rc, out = run_cli("--file", os.path.join(TMP, "case6i.md"),
                      "--text-file", fffd_in, "--allow-replacement")
    check("6i --allow-replacement 放行，exit 0", rc == 0, (rc, out[:160]))

    # ---------------------------------------------------------------- ⑦ 正常流程 0 影响
    md = os.path.join(TMP, "normal.md")
    plan = mod.write_text(md, "# 标题\n\n正文一行。\n")
    raw = open(md, "rb").read()
    check("⑤a 正常 .md：UTF-8 无 BOM", not raw.startswith(b"\xef\xbb\xbf"))
    check("⑤b 正常 .md：字节数与 sha1 自读一致", plan["verified"] and plan["bytes"] == len(raw))
    check("⑤c 正常 .md：体检干净", mod.inspect(md) == [], mod.inspect(md))

    # 原子性：临时文件不许留在目录里
    leftovers = [n for n in os.listdir(TMP) if n.startswith(".safe_write-")]
    check("⑤d 不留临时文件", leftovers == [], leftovers)

    # 覆盖已有文件 + --backup：备份必须存在且是旧内容
    with open(md, "wb") as fh:
        fh.write(b"old\n")
    plan = mod.write_text(md, "new\n", backup=True)
    check("⑤e --backup 生成可回滚的备份",
          plan["backup"] and os.path.exists(plan["backup"])
          and open(plan["backup"], "rb").read() == b"old\n")

    print()
    print("临时目录：", TMP)
    print("FAILED:", fails or "none")
    sys.exit(1 if fails else 0)
