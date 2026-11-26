# -*- coding: utf-8 -*-
r"""飞书助手回话给桌面助手：一条命令，不用记 sessionId、不用手填 id。

为什么要这个（2026-11-12 14:4x，用户点的问题）
    用户关心的是"没他在的时候，两个窗口互通的稳定性"——尤其两种情况：
      ① 桌面助手开了新对话 → 桌面助手的 sessionId 变了，她记着的旧 id 就失效；
      ② 她忘了该怎么访问桌面助手 → 于是乱试（实测：她用 send_to_umo 填了
         'bot:FriendMessage:10000'，把港湾端口号当成了 QQ 号，消息直接丢了）。
    结论：指引不能靠"提醒"，得变成一条她随手就能跑、且不会记错的命令。

2026-11-17 首批修复（A01）改了什么
    原来第 2 步是 GET /api/harbor/latest?cwd=… ——**它只按活跃时间挑**，
    分不清飞书助手与桌面助手：10:47 它返回飞书助手的会话、11:2x 返回桌面助手的会话，
    于是"她刚回完用户、我正好空闲"这个最常见的时刻，她会把话投给自己。
    现在改成**按身份解析**：
      - 身份：session_identity 读原始会话头（agentPreset / origin / parentSession /
        delegationDepth）判定，不是看文件夹名、也不是"不在飞书助手映射里就算桌面助手"；
      - 存活：由港湾插件判（live 只有宿主知道）；
      - 唯一合格才投；两个以上直接报歧义（退出码 6），**不替你挑**；
      - 显式 --session-id 也要过身份关，不能投到飞书助手自己、委派会话或未确认会话。

2026-11-17 首批回修（R1/R2）又改了两处
    R1：三条路径（显式 / 绑定 / 唯一）都改成先 verify_target()——插件按目标**当前头**判角色、
        并确认它此刻还活着；缓存只说明"生成那一刻它像谁"。
    R2：回话优先沿**来件关联**回去——桌面助手哪个窗口发来的，就回那个窗口；
        插件受理时把 (to, from) 写进 工作台\_tmp_\来件关联.jsonl。关联失效就明确报
        origin_unavailable（退出码 7），**不自动改投另一个窗口**；超过 30 分钟不再算"回话"。

它做什么
    1. 读正本 通道.json 拿桌面助手侧的 host / port / token / session_cwd（不写死）。
    2. 解析目标会话：显式 > 来信关联 > 一次性绑定 > 唯一合格。
    3. POST /api/harbor/say 投递（mode 默认 steer：插到桌面助手下一步），带上 from=我的会话 id。
       **HTTP 200 还要看业务 ok 与 accepted**——HTTP 成功不等于投递成功。
    4. 写回执 工作台\桌面助手投递回执.json（失败也留痕，连原因一起记：stage＋reason；
       回执写不进去会明说，不静默吞掉，也不因此自动重发）。

用法
    python tools\reach_dsh.py --file 正文.txt          # 长文本/中文多，一律走文件
    python tools\reach_dsh.py --text "一句话"
    python tools\reach_dsh.py --file 正文.txt --mode queue   # 排队到他这一轮之后
    python tools\reach_dsh.py --dry-run --file 正文.txt      # 只看会投给谁，不真发
    python tools\reach_dsh.py --whoami                      # 只报解析结果与依据
    python tools\reach_dsh.py --session-id <id> --text "…"   # 明确指定（照样验身份）
    # 发件人默认取 $env:DSH_SESSION_ID（宿主 shell 里有）；取不到才要 --from

退出码
    0 已受理（accepted 为真）
    2 环境问题（正本读不到、网络不通、参数错、正文空）
    3 没有合格目标（他可能没开窗口）
    4 口令不对（401）
    5 投递失败或业务未受理（HTTP 到了但 ok/accepted 不是真）
    6 目标歧义（多个合格会话，需明确指定或登记一次绑定）
    7 身份问题（缓存不可用/目标未确认/角色不符/绑定失效）

依据：接口语义见 tools\plugins\dsh-plugin-harbor\lib\index.js 与 lib\identity.js；
      身份判据见 tools\session_identity.py 的 classify()。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ask_card  # noqa: E402
import channel_params as CP  # noqa: E402
import msg_envelope as ME  # noqa: E402

import delivery_receipt as DR  # noqa: E402  回执：送到没、为什么没送到


def _ask_desktop_via_card(m: dict, got: dict, timeout: float = 300.0) -> dict | None:
    """当目标缺失、归档、绑定失效或歧义时，调系统卡片服务请用户选窗、唤起 DSH 新建或重扫。"""
    def _build_groups(cands: list[dict], err_reason: str) -> list[dict]:
        groups: list[dict] = []
        if cands:
            opts = []
            for idx, c in enumerate(cands, 1):
                title = (c.get("title") or "").strip()
                created = str(c.get("createdAt") or "").strip()
                label = title or ("桌面助手对话 #%d" % idx)
                desc = ("创建于 %s" % created) if created else "活跃 DSH 桌面窗口"
                opts.append({
                    "id": c.get("sessionId") or "",
                    "label": label,
                    "description": desc,
                    "tags": ["活跃"],
                    "action": "pick",
                })
            groups.append({"title": "已有活跃 DSH 窗口", "kind": "session", "options": opts})
        groups.append({
            "title": "其他操作" if cands else "当前没有活跃的 DSH 窗口（%s）" % err_reason,
            "kind": "session",
            "options": [
                {
                    "id": "__new__:dsh",
                    "label": "＋ 一键在 DSH 新建对话并投递",
                    "description": "优先通过港湾直接建新会话并投递；若港湾未就绪则拉起 DSH 到前台",
                    "tags": ["新建"],
                    "action": "new_window",
                    "app": "dsh",
                },
                {
                    "id": "__rescan__",
                    "label": "我已在 DSH 打开新窗口，重新扫描并投递",
                    "description": "重新刷新身份并扫描当前活跃的 DSH 窗口",
                    "action": "rescan",
                },
                {
                    "id": "__cancel__",
                    "label": "暂不投递（取消）",
                    "description": "本次不发送，稍后再试",
                    "action": "cancel",
                },
            ],
        })
        return groups

    cur_got = got
    for _attempt in range(5):
        cands = [c for c in (cur_got.get("candidates") or []) if isinstance(c, dict) and c.get("sessionId")]
        err_reason = str(cur_got.get("verify_error") or cur_got.get("error") or "需要选窗")
        picked = ask_card.choose_from_groups(
            _build_groups(cands, err_reason),
            timeout=timeout,
            title="选择接收消息的 DSH 窗口",
            subtitle="旧绑定已不可用或存在多个窗口，请点选目标、唤起 DSH 新建对话，或开窗后点重新扫描。",
            asker="reach_dsh",
        )
        if not picked or picked.get("action") == "cancel":
            return None
        if picked.get("action") == "pick" and picked.get("id"):
            return CP.resolve_desktop_session(m, explicit=str(picked["id"]))
        if picked.get("action") == "new_window":
            created_sid = str(picked.get("created_session_id") or "").strip()
            if created_sid:
                print("已在 DSH 自动新建会话 %s…，正在刷新身份并直接投递…" % created_sid[:20])
                for wait_i in range(3):
                    try:
                        CP.refresh_identity(force=True)
                    except Exception:
                        pass
                    res = CP.resolve_desktop_session(m, explicit=created_sid)
                    if res.get("ok"):
                        return res
                    if wait_i < 2:
                        time.sleep(0.3)
                cur_got = res
                continue
            if picked.get("desktop_draft_requested") is True:
                print("已通过 dsh://open 请求唤起 DSH 桌面端；请在 DSH 新建对话后，在下一张卡片点「重新扫描并投递」。")
            else:
                print("宿主卡片服务尚未确认唤起 DSH（如刚更新 card_service.py，请由宿主机重启卡片服务；或手动切到 DSH 新建对话后再点重新扫描）。")
            continue
        if picked.get("action") == "rescan":
            try:
                CP.refresh_identity(force=True)
            except Exception:
                pass
            res = CP.resolve_desktop_session(m, explicit="")
            if res.get("ok"):
                return res
            new_cands = [c for c in (res.get("candidates") or []) if isinstance(c, dict) and c.get("sessionId")]
            if len(new_cands) == 1:
                v = CP.resolve_desktop_session(m, explicit=str(new_cands[0]["sessionId"]))
                if v.get("ok"):
                    return v
            cur_got = res
            continue
    return None

RECEIPT = str(DR.receipt_path("desktop_bot"))  # 兼容旧读法；写入走 DR.record


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_QUERY = {"on": False}


def record(entry):
    """查询模式（--whoami／--dry-run）不写回执：那不是投递，别把回执当操作日志。"""
    if _QUERY["on"]:
        return False
    return DR.record("desktop_bot", entry)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="飞书助手回话给桌面助手：按身份找目标，不猜最新")
    ap.add_argument("--text", help="要说的话")
    ap.add_argument("--file", help="从文件读正文（推荐，长文本/中文多时用）")
    ap.add_argument("--mode", choices=["steer", "queue"], default="steer",
                    help="steer=插到他下一步（默认）；queue=排到这一轮之后")
    ap.add_argument("--dry-run", action="store_true", help="只报会投给哪个会话，不真发")
    ap.add_argument("--whoami", action="store_true", help="只报解析结果与依据")
    ap.add_argument("--session-id", help="明确指定目标会话（仍然要过身份校验）")
    ap.add_argument("--task-id", default="", help="同一件事的号（可选；不填就不带）")
    ap.add_argument("--reply-to", default="", help="回的是哪一条 msgId（可选）")
    ap.add_argument("--from", dest="from_sid",
                    help="发件方会话 id（默认取 DSH_SESSION_ID；用来把回话关联回原窗口）")
    ap.add_argument("--origin", default="",
                    help="发件同僚 AI 名（如 review_bot/gpt）；正文首行加 ORIGIN 信任名牌（P1-3）")
    ap.add_argument("--ask", action="store_true",
                    help="目标缺失/归档/歧义时弹系统选窗卡片请用户点选、唤起 DSH 或重扫")
    ap.add_argument("--card-timeout", type=float, default=300.0,
                    help="等待选窗卡片的秒数（默认 300）")
    args = ap.parse_args(argv)
    _QUERY["on"] = bool(getattr(args, "whoami", False) or getattr(args, "dry_run", False))

    try:
        m = CP.desktop_bot()
    except Exception as exc:  # noqa: BLE001
        print("读不到通道正本（或通道被 policy 关着）：%s" % exc)
        print("  正本：_state\\通道.json；自检：python tools\\channel_params.py")
        return 2

    got = CP.resolve_desktop_session(m, explicit=(args.session_id or "").strip())
    if (not got.get("ok")) and args.ask and not (args.session_id or "").strip():
        err_try = str(got.get("error") or "")
        if err_try in ("binding_invalid", "ambiguous", "target_archived", "no_target", "no session"):
            try:
                picked_got = _ask_desktop_via_card(m, got, timeout=args.card_timeout)
            except Exception as exc:  # noqa: BLE001
                print("调系统选窗卡片失败：%s" % exc)
                picked_got = None
            if picked_got is not None:
                got = picked_got
    if not got.get("ok"):
        err = str(got.get("error") or "")
        detail = str(got.get("detail") or "")
        base = {"at": _stamp(), "ok": False, "error": err, "detail": detail}
        if got.get("http") == 401:
            print("口令不对（401）。查 通道.json 的 desktop_bot.token；改过 token 需要重启对应一侧才生效。")
            record(dict(base, http=401, stage="resolve",
                               reason="unauthorized", result_known=True))
            return 4
        if err == "harbor_not_loaded":
            print("港湾插件没加载：%s" % (detail or "/api/harbor/target 404"))
            print("  重启 DSH 让它加载；加载后先跑 python tools\\channel_params.py --live 确认。")
            record(dict(base, stage="resolve", reason=DR.normalize(err),
                               result_known=True))
            return 2
        if err in ("identity_unavailable", "identity_refresh_failed"):
            print("身份缓存不可用：%s" % (detail or err))
            print("  先跑：python tools\\session_identity.py --refresh")
            record(dict(base, stage="resolve", reason=DR.normalize(err),
                               result_known=True))
            return 7
        if err == "origin_unavailable":
            print("这条回不到原来信窗口：%s" % (detail or "那个窗口已不可投"))
            print("  不自动改投别的窗口（那会把回话送错人）。"
                  "确认一下那个窗口是不是关了；要发新的一条，就明确给 --session-id。")
            record(dict(base, origin=got.get("origin"), stage="resolve",
                               reason=DR.normalize(err), result_known=True))
            return 7
        if err == "target_archived":
            print("目标会话已归档，绝不投递：%s" % (detail or got.get("sessionId")))
            print("  归档窗不可投；让收件方开新窗口，或更新绑定／明确 --session-id。")
            record(dict(base, stage="resolve", reason="target_archived", result_known=True))
            return 7
        if err == "binding_invalid" and (got.get("candidates") or got.get("needs_confirm")):
            print("绑定窗已不可用（%s），剔除归档后检测到新活候选，需人确认（不自动挑）："
                  % (got.get("verify_error") or err))
            for c in (got.get("candidates") or []):
                t = (c.get("title") or "").strip()
                t_part = f"「{t}」 " if t else ""
                print("  候选 %s%s（创建 %s）" % (t_part, c.get("sessionId"), c.get("createdAt")))
            if not (got.get("candidates") or []):
                print("  （当前没有合格活候选；等收件方开窗口后再投。）")
            print("  明确给 --session-id，或在 通道.json 更新 desktop_bot.bound_session。")
            record(dict(base, candidates=got.get("candidates"), stage="resolve",
                               reason="binding_invalid_needs_confirm", result_known=True))
            return 6 if (got.get("candidates") or []) else 7
        if err in ("target_unconfirmed", "target_role_mismatch", "binding_invalid",
                   "target_delegated", "target_not_live", "identity_conflict"):
            print("目标身份不合格：%s" % (detail or err))
            print("  这一条不会自动改投别人；确认是哪个窗口后再说。")
            record(dict(base, stage="resolve", reason=DR.normalize(err),
                               result_known=True))
            return 7
        if err == "ambiguous":
            print("目标歧义：%s" % (detail or "有多个合格会话"))
            for c in (got.get("candidates") or []):
                t = (c.get("title") or "").strip()
                t_part = f"「{t}」 " if t else ""
                print("  候选 %s%s" % (t_part, c.get("sessionId")))
            print("  要我投哪个，得明确给 --session-id，或在 通道.json 登记一次 desktop_bot.bound_session。")
            record(dict(base, candidates=got.get("candidates"), stage="resolve",
                               reason="ambiguous", result_known=True))
            return 6
        if err in ("no_target", "no session"):
            print("没有合格的桌面助手目标会话：%s" % (detail or err))
            for s in (got.get("seen") or []):
                print("  活着但身份是 %s：%s" % (s.get("role"), s.get("sessionId")))
            print("  他可能没开窗口。等他在的时候再投。")
            record(dict(base, stage="resolve", reason=DR.normalize(err),
                               result_known=True))
            return 3
        print("解析桌面助手目标失败：%s%s" % (err, ("｜" + detail) if detail else ""))
        print("  自检：python tools\\channel_params.py")
        record(dict(base, stage="resolve", reason=DR.normalize(err),
                           result_known=True))
        return 3

    sid = got["sessionId"]
    basis = got.get("basis") or []
    print("目标会话：%s…（依据 %s）" % (sid[:20], ", ".join(basis) or "unknown"))
    print("身份：%s（%s）" % (got.get("role"),
                            "按当前头判" if got.get("source") == "live-header" else "按缓存判"))
    if "auto_switch_after_binding_invalid" in basis:
        print("提示：原绑定窗 %s… 已不可用，剔除归档后唯一活候选且 cwd 相符，已自动切换（智能无歧义情形）。"
              % str(got.get("auto_switched_from") or "")[:20])

    if args.whoami or args.dry_run:
        if args.dry_run:
            print("（dry-run，未投递）")
        return 0

    text = args.text or ""
    if args.file:
        try:
            with open(args.file, encoding="utf-8-sig") as f:
                text = f.read()
        except OSError as exc:
            print("读不到正文文件：%s" % exc)
            return 2
    text = (text or "").strip()
    if not text:
        print("要有话可说：给 --text 或 --file")
        return 2

    sender = (args.from_sid or CP.my_session_id() or "").strip()
    env = ME.new_envelope(task_id=getattr(args, "task_id", ""), reply_to=getattr(args, "reply_to", ""), peer_agent=getattr(args, "origin", ""))
    text = ME.wrap(text, env)
    payload = {"sessionId": sid, "text": text, "mode": args.mode, "expect": "desktop"}
    if sender:
        payload["from"] = sender
    payload.update(env)
    status, resp = CP.post_json_soft(m["base"] + "/api/harbor/say", m["token"], payload)
    resp = resp or {}
    stock = {"at": _stamp(), "targetSessionId": sid, "role": got.get("role"),
             "basis": basis, "mode": args.mode, "chars": len(text), "http": status,
             "msgId": env["msgId"], "taskId": env.get("taskId"), "replyTo": env.get("replyTo"),
             "peerAgent": env.get("peerAgent")}

    if status == 0:
        # 连都没连上：结果**未知**（可能已经到了，也可能没到）
        print("投递结果未知：连接没成功（%s）" % (resp.get("error") or "no response"))
        print("  不自动重发；先确认对方有没有收到。")
        record(dict(stock, ok=False, result_known=False,
                           stage="send", reason="connection_failed"))
        return 2
    if status == 401:
        print("口令不对（401）。查 通道.json 的 desktop_bot.token。")
        record(dict(stock, ok=False, result_known=True, http=401,
                           stage="send", reason="unauthorized"))
        return 4

    ok = bool(resp.get("ok")) and bool(resp.get("accepted"))
    if ok:
        print("已受理：HTTP %s，accepted=True，mode=%s，%d 字（对方尚未回复，不等于已完成）"
              % (status, resp.get("mode"), len(text)))
        record(dict(stock, ok=True, accepted=True, result_known=True,
                           role=resp.get("role") or got.get("role"),
                           stage="send", reason="accepted"))
        return 0

    # HTTP 200 但业务 false / 缺字段：不能报成功
    print("投递未受理：HTTP %s，error=%s%s"
          % (status, resp.get("error") or "（缺字段）",
             ("｜" + str(resp.get("detail"))) if resp.get("detail") else ""))
    if resp.get("candidates"):
        for c in resp["candidates"]:
            print("  候选 %s" % c.get("sessionId"))
    record(dict(stock, ok=False, accepted=resp.get("accepted"),
                       result_known=True, error=resp.get("error"),
                       stage="send", reason=DR.classify(status, resp)[3]))
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
