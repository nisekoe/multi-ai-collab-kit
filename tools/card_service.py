#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统级选窗卡片服务 v4（常驻、右下角分组小卡）。

结构：主标题 → 若干「分组」→ 每组一个浅底大框，框里是若干卡片条（整条可点）。
分组是自创卡片才有的自由度（原来只能全堆一块）；只有一组也照常显示。
接口（只绑 127.0.0.1:8790）：
  GET  /api/pending
  POST /api/request  {title, subtitle, groups:[{title, options:[{label,description,id}]}], asker}
                     兼容旧格式：{title, options:[...]} 会包成一组
  POST /api/answer   {id, choice}   choice 是「按组展开后的全局序号」
  GET  /api/result?id=

依据：用户 2026-11-23「按分组来，一组一个框，框里卡片条」；实现 Tkinter＋Win32 圆角；配色自拟。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CARD_DIR = Path(os.environ.get("CARD_DIR")
                or (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / ".multi-ai-collab-kit" / "cards"))
IS_WINDOWS = os.name == "nt"
HOST = "127.0.0.1"  # 卡片服务没有鉴权，固定只听本机；端口可用 CARD_PORT 改
DEFAULT_PORT = 8790
_inbox: "queue.Queue[dict]" = queue.Queue()

BG = "#FAFBFD"
FG = "#1E293B"
SUB = "#64748B"
MUTED = "#64748B"
GROUP_BG = "#EEF3FC"
GROUP_TITLE = "#334155"
ROW = "#FFFFFF"
ROW_HOVER = "#EEF4FF"
BORDER = "#E4EAF2"
ACCENT = "#5C7CFA"
MAX_PER_GROUP = 10


def _dpi_aware() -> None:
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _round_region(hwnd: int, w: int, h: int, r: int = 34) -> None:
    try:
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r)
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def _tag_colors(text: str) -> tuple[str, str]:
    """小标记的配色：按关键词给底色/字色（柔雾莫兰迪风）。"""
    t = text or ""
    if "最近活跃" in t:
        return "#FFF1E0", "#B95000"
    if any(k in t for k in ("归档", "旧", "已结束")):
        return "#EFF2F6", "#64748B"
    if any(k in t for k in ("云端", "cloud")):
        return "#F2EEFF", "#6741D9"
    if any(k in t for k in ("保护", "禁止")):
        return "#FFEBEB", "#C92A2A"
    if any(k in t for k in ("跑", "占用", "忙", "running", "冲突")):
        return "#FFF4DC", "#AD5A00"
    if any(k in t for k in ("最近", "新鲜", "活跃", "idle", "空闲")):
        return "#E8F7EF", "#16794F"
    if any(k in t for k in ("项目", "proj")):
        return "#EAF0FF", "#3B5BDB"
    return "#F1F5F9", "#475569"


def _norm(data: dict) -> list[dict]:
    groups = data.get("groups")
    if not groups:
        groups = [{"title": data.get("group") or "选项", "options": data.get("options") or []}]
    out = []
    for g in groups:
        opts = [o for o in (g.get("options") or []) if isinstance(o, dict)]
        if opts:
            out.append({"title": g.get("title") or "", "kind": g.get("kind") or "", "q": g.get("q") or "", "options": opts})
    return out


def _flat(groups: list[dict]) -> list[dict]:
    return [o for g in groups for o in g["options"]]


def _soft_shadow(hwnd: int) -> None:
    if not IS_WINDOWS:
        return
    """给无边框窗口加一层系统投影（CS_DROPSHADOW），看起来不那么硬。"""
    try:
        GCL_STYLE, CS_DROPSHADOW = -26, 0x00020000
        style = ctypes.windll.user32.GetClassLongPtrW(hwnd, GCL_STYLE)
        ctypes.windll.user32.SetClassLongPtrW(hwnd, GCL_STYLE, style | CS_DROPSHADOW)
    except Exception:
        pass


def _fade_close(win, stack: list, ms: int = 7) -> None:
    """收起时淡出＋下沉一点，不硬切。"""
    try:
        x, y = win.winfo_x(), win.winfo_y()
        w, h = win.winfo_width(), win.winfo_height()
    except Exception:
        try:
            win.destroy()
        except Exception:
            pass
        return

    def step(i: int):
        if i <= 0:
            try:
                win.destroy()
            except Exception:
                pass
            if win in stack:
                stack.remove(win)
            return
        try:
            win.attributes("-alpha", max(0.0, 0.95 * i / ms))
            win.geometry(f"{w}x{h}+{x}+{y + int(8 * (ms - i) / ms)}")
        except Exception:
            pass
        win.after(16, lambda: step(i - 1))

    step(ms)


def _write(rec: dict) -> None:
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    (CARD_DIR / f"{rec['id']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(qid: str) -> dict | None:
    p = CARD_DIR / f"{qid}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pending() -> list[dict]:
    if not CARD_DIR.is_dir():
        return []
    out = []
    for p in CARD_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("status") == "pending":
            out.append(d)
    return sorted(out, key=lambda d: d.get("created", ""))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/pending"):
            return self._send(200, json.dumps(_pending(), ensure_ascii=False).encode())
        if self.path.startswith("/api/result"):
            qid = self.path.split("id=", 1)[-1] if "id=" in self.path else ""
            rec = _read(qid)
            return self._send(200 if rec else 404, json.dumps(rec or {}, ensure_ascii=False).encode())
        if self.path in ("/", "/index.html"):
            parts = ["<!doctype html><meta charset=utf-8><title>选窗卡片</title>",
                     "<style>body{font-family:system-ui;max-width:680px;margin:30px auto}.g{border:1px solid #e4e7ec;"
                     "border-radius:12px;padding:10px;margin:10px 0;background:#f7f8fa}.gt{color:#475467;font-size:13px;"
                     "margin:4px 6px 8px}.o{display:block;width:100%;text-align:left;border:1px solid #e4e7ec;border-radius:8px;"
                     "background:#fff;padding:10px 12px;margin:6px 0;cursor:pointer}.m{color:#888;font-size:12px}</style>"]
            for it in _pending():
                groups = it.get("groups") or _norm(it)
                html = [f'<div class="c"><b>{it.get("title","")}</b><div class="m">{it.get("asker","")}</div>']
                idx = 0
                for g in groups:
                    html.append(f'<div class="g"><div class="gt">{g.get("title","")}</div>')
                    for o in g["options"]:
                        html.append(f'<button class="o" onclick="ans(\'{it["id"]}\',{idx})">{o.get("label","")}'
                                    f'<div class="m">{o.get("description","")}</div></button>')
                        idx += 1
                    html.append("</div>")
                html.append("</div>")
                parts.append("".join(html))
            parts.append("<script>async function ans(id,i){await fetch('/api/answer',{method:'POST',"
                         "headers:{'Content-Type':'application/json'},body:JSON.stringify({id,choice:i})});location.reload()}</script>")
            return self._send(200, "".join(parts).encode("utf-8"), "text/html; charset=utf-8")
        return self._send(404, b"{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, b'{"error":"bad json"}')
        if self.path.startswith("/api/request"):
            qid = data.get("id") or ("c" + secrets.token_hex(4))
            groups = _norm(data)
            rec = {"id": qid, "title": data.get("title") or "选哪一个？",
                   "subtitle": data.get("subtitle") or data.get("question") or "",
                   "groups": groups, "flat": _flat(groups),
                   "asker": data.get("asker") or "", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "status": "pending", "choice": None}
            _write(rec)
            _inbox.put(rec)
            return self._send(200, json.dumps({"id": qid}, ensure_ascii=False).encode())
        if self.path.startswith("/api/answer"):
            qid = str(data.get("id") or "")
            rec = _read(qid)
            if not rec:
                return self._send(404, b'{"error":"missing"}')
            upd = {"status": "answered", "answered": time.strftime("%Y-%m-%d %H:%M:%S")}
            if "choices" in data:
                upd["choices"] = data.get("choices")
            else:
                upd["choice"] = data.get("choice")
            rec.update(upd)
            _write(rec)
            return self._send(200, b'{"ok":true}')
        return self._send(404, b"{}")


def serve(port: int) -> None:
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()


def popups() -> int:
    import tkinter as tk
    import tkinter.font as tkfont

    _dpi_aware()
    root = tk.Tk()
    root.withdraw()
    root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    fam = "Microsoft YaHei UI"
    f_title = tkfont.Font(root=root, family=fam, size=14, weight="bold")
    f_sub = tkfont.Font(root=root, family=fam, size=10)
    f_group = tkfont.Font(root=root, family=fam, size=10, weight="bold")
    f_opt = tkfont.Font(root=root, family=fam, size=11, weight="bold")
    f_desc = tkfont.Font(root=root, family=fam, size=10)
    f_pill = tkfont.Font(root=root, family=fam, size=9)
    f_badge = tkfont.Font(root=root, family=fam, size=10, weight="bold")
    f_foot = tkfont.Font(root=root, family=fam, size=11, weight="bold")
    stack: list[tk.Toplevel] = []
    live: dict[str, tk.Toplevel] = {}

    def answer(rec: dict, idx: int, win: tk.Toplevel):
        rec.update(status="answered", choice=idx, answered=time.strftime("%Y-%m-%d %H:%M:%S"))
        _write(rec)
        _fade_close(win, stack)

    def build(rec: dict):
        win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        canvas = tk.Canvas(win, bg=BG, highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)

        W = 470
        pad = 20
        inner_w = W - 2 * pad
        y = 18

        def rr(x1, y1, x2, y2, r, fill, outline="", width=1):
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            return canvas.create_polygon(pts, smooth=True, splinesteps=24, fill=fill,
                                          outline=outline, width=width)

        def pill(x, y0, text, bg, fg, font=None):
            fnt = font or f_pill
            hgt = 22 if fnt is f_badge else 20
            w = fnt.measure(text) + 18
            rid = rr(x, y0, x + w, y0 + hgt, hgt // 2, bg)
            tid = canvas.create_text(x + w / 2, y0 + hgt / 2, text=text, fill=fg, font=fnt)
            return rid, tid, x + w + 6

        title = rec.get("title") or "选哪一个？"
        tid = canvas.create_text(pad, y, text=title, fill=FG, font=f_title, anchor="nw", width=inner_w)
        y = canvas.bbox(tid)[3] + 4
        sub = rec.get("subtitle") or rec.get("asker") or ""
        if sub:
            sid = canvas.create_text(pad, y, text=str(sub), fill=SUB, font=f_sub, anchor="nw", width=inner_w)
            y = canvas.bbox(sid)[3] + 10
        else:
            y += 4

        raw_groups = rec.get("groups") or []
        qs = []
        for g in raw_groups:
            q = g.get("q") or ""
            if q not in qs:
                qs.append(q)
        multi = len(qs) > 1
        sel = {}
        drawn = []
        flat_idx = 0
        for g in raw_groups:
            q = g.get("q") or ""
            kind = g.get("kind") or ("session" if str(g.get("title") or "").startswith("会话区") else "project")
            accent = "#6488EA" if kind == "project" else "#B8A99A"
            row_outline = "#E1E9F7" if kind == "project" else "#EAE3D9"
            row_shadow = "#DCE5F5" if kind == "project" else "#E6DDD0"
            group_top = y
            rr(pad, y + 2, pad + 4, y + 16, 2, accent)
            gx = pad + 11
            gt = canvas.create_text(gx, y + 9, text=g.get("title") or "", fill=GROUP_TITLE, font=f_group, anchor="w")
            gx = canvas.bbox(gt)[2] + 8
            b_text = "项目" if kind == "project" else "无分类对话"
            b_bg, b_fg = ("#E2ECFF", "#3B5BDB") if kind == "project" else ("#EFEAE2", "#64748B")
            _, _, gx = pill(gx, y - 2, b_text, b_bg, b_fg, f_badge)
            n = len(g.get("options") or [])
            canvas.create_text(W - pad, y + 9, text=f"{n} 条", fill=SUB, font=f_desc, anchor="e")
            y += 34

            for o in (g.get("options") or [])[:MAX_PER_GROUP]:
                row_top = y
                lab = canvas.create_text(pad + 14, row_top + 11, text=o.get("label") or "", fill=FG,
                                         font=f_opt, anchor="nw", width=inner_w - 28)
                y = canvas.bbox(lab)[3] + 5
                tags = o.get("tags") or []
                if tags:
                    tx = pad + 14
                    for t in tags[:4]:
                        tbg, tfg = _tag_colors(str(t))
                        _, _, tx = pill(tx, y, str(t), tbg, tfg)
                    y += 24
                desc = o.get("description") or ""
                if desc:
                    dt = canvas.create_text(pad + 14, y, text=desc, fill=SUB, font=f_desc,
                                            anchor="nw", width=inner_w - 28)
                    y = canvas.bbox(dt)[3] + 10
                else:
                    y += 6
                row_bottom = y
                rid = rr(pad, row_top, W - pad, row_bottom, 16, ROW, row_outline)
                canvas.tag_lower(rid)
                shadow = rr(pad, row_top + 2, W - pad, row_bottom + 2, 16, row_shadow, "")
                canvas.tag_lower(shadow, rid)
                tag = f"row{flat_idx}"
                canvas.addtag_withtag(tag, lab)
                canvas.addtag_withtag(tag, rid)
                for item in canvas.find_overlapping(pad, row_top, W - pad, row_bottom):
                    canvas.addtag_withtag(tag, item)
                k = flat_idx
                if multi:
                    canvas.tag_bind(tag, "<Button-1>", lambda e, qq=q, i=k: pick(qq, i))
                else:
                    canvas.tag_bind(tag, "<Button-1>", lambda e, r=rec, i=k, w=win: answer(r, i, w))
                canvas.tag_bind(tag, "<Enter>", lambda e, iid=rid: (canvas.config(cursor="hand2"), canvas.itemconfig(iid, outline="#748FFC", width=2)))
                canvas.tag_bind(tag, "<Leave>", lambda e, qq=q, i=k, iid=rid, ro=row_outline: (canvas.config(cursor=""),
                                canvas.itemconfig(iid, **({"fill": ROW_HOVER, "outline": "#5C7CFA", "width": 2} if sel.get(qq) == i else {"fill": ROW, "outline": ro, "width": 1}))))
                drawn.append((q, k, rid, row_outline))
                y = row_bottom + 9
                flat_idx += 1

            box_fill = "#EEF3FC" if kind == "project" else "#F7F2EC"
            box_outline = "#DCE6F8" if kind == "project" else "#EBE2D6"
            box = rr(pad - 12, group_top - 10, W - pad + 12, y + 6, 24, box_fill, box_outline)
            canvas.tag_lower(box)
            y += 30

        def _paint():
            for q, fi, iid, ro in drawn:
                if sel.get(q) == fi:
                    canvas.itemconfig(iid, fill=ROW_HOVER, outline="#5C7CFA", width=2)
                else:
                    canvas.itemconfig(iid, fill=ROW, outline=ro, width=1)

        def pick(q: str, fi: int):
            sel[q] = fi
            _paint()

        def submit():
            if len(sel) < len(qs):
                if tip:
                    canvas.itemconfig(tip, text="上面还有没选的")
                return
            choices = [sel[q] for q in qs]
            rec.update(status="answered", choices=choices, choice=None,
                       answered=time.strftime("%Y-%m-%d %H:%M:%S"))
            _write(rec)
            _fade_close(win, stack)

        tip = None
        if multi:
            tip = canvas.create_text(pad, y, text="每组选一个，最后点确定", fill="#475569", font=f_foot, anchor="nw")
            ok = canvas.create_text(W - pad, y, text="确定", fill="#3B5BDB", font=f_foot, anchor="ne")
            canvas.tag_bind(ok, "<Button-1>", lambda e: submit())
            canvas.tag_bind(ok, "<Enter>", lambda e: (canvas.itemconfig(ok, fill="#2B4ACB"), canvas.config(cursor="hand2")))
            canvas.tag_bind(ok, "<Leave>", lambda e: (canvas.itemconfig(ok, fill="#3B5BDB"), canvas.config(cursor="")))
            close_id = canvas.create_text(W - pad - 78, y, text="稍后", fill="#64748B", font=f_foot, anchor="ne")
        else:
            canvas.create_text(pad, y, text="点一条即可继续", fill="#475569", font=f_foot, anchor="nw")
            close_id = canvas.create_text(W - pad, y, text="稍后", fill="#64748B", font=f_foot, anchor="ne")
        canvas.tag_bind(close_id, "<Button-1>", lambda e, w=win: _fade_close(w, stack))
        canvas.tag_bind(close_id, "<Enter>", lambda e: (canvas.itemconfig(close_id, fill=FG), canvas.config(cursor="hand2")))
        canvas.tag_bind(close_id, "<Leave>", lambda e: (canvas.itemconfig(close_id, fill="#64748B"), canvas.config(cursor="")))
        y += 38

        w, h = W, int(y)
        canvas.config(width=w, height=h)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        yy = sh - h - 120 - sum(x.winfo_height() + 10 for x in stack)
        xx = sw - w - 24
        win.geometry(f"{w}x{h}+{max(0, xx)}+{max(0, yy) + 14}")
        try:
            win.attributes("-alpha", 0.0)
        except Exception:
            pass
        win.deiconify()
        _round_region(win.winfo_id(), w, h)
        _soft_shadow(win.winfo_id())
        try:
            win.lift()
            win.attributes("-topmost", True)
            ctypes.windll.user32.SetWindowPos(win.winfo_id(), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        except Exception:
            pass
        steps = 9

        def _step(i: int = 1):
            try:
                win.attributes("-alpha", min(0.98, 0.05 + 0.93 * i / steps))
                win.geometry(f"{w}x{h}+{max(0, xx)}+{max(0, yy) + int(14 * (1 - i / steps))}")
            except Exception:
                return
            if i < steps:
                win.after(22, lambda: _step(i + 1))

        win.after(10, _step)
        stack.append(win)
        live[rec.get("id", "")] = win

    def poll():
        try:
            while True:
                build(_inbox.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            import traceback
            try:
                _logdir = CARD_DIR.parent / "logs"
                _logdir.mkdir(parents=True, exist_ok=True)
                (_logdir / "card_error.log").write_text(
                    time.strftime("%Y-%m-%d %H:%M:%S\n") + traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass
        root.after(400, poll)

    def sweep():
        ttl = 600.0  # 10 分钟没人理就过期
        now = time.time()
        for cid, win in list(live.items()):
            rec = _read(cid)
            if not rec or rec.get("status") != "pending":
                live.pop(cid, None)
                continue
            try:
                age = now - time.mktime(time.strptime(rec.get("created", ""), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                continue
            if age > ttl:
                rec["status"] = "expired"
                rec["answered"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _write(rec)
                live.pop(cid, None)
                _fade_close(win, stack)
        root.after(60000, sweep)

    root.after(5000, sweep)
    root.after(300, poll)
    root.mainloop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="系统级选窗卡片服务")
    ap.add_argument("--port", type=int, default=os.environ.get("CARD_PORT", str(DEFAULT_PORT)))
    ap.add_argument("--no-popup", action="store_true")
    a = ap.parse_args()
    if not 1 <= a.port <= 65535:
        ap.error("--port 或 CARD_PORT 必须在 1–65535 之间")
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=serve, args=(a.port,), daemon=True).start()
    print(f"卡片服务 http://{HOST}:{a.port}/", flush=True)
    if a.no_popup:
        while True:
            time.sleep(3600)
    try:
        return popups()
    except Exception as exc:
        print("弹窗不可用，退回网页模式：", exc, flush=True)
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
