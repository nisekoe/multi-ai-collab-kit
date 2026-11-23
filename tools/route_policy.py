# -*- coding: utf-8 -*-
"""路由判定（机器闸的决策层）：输入一条线的事实，输出「怎么投」的结论。

设计依据：GPT 2026-11-23 审校（B 节顺序与退出语义）＋用户当天定的回程地址制；
四类必须确认：冷启动无地址、地址失效且要换窗、点名重名/绑定冲突、新窗疑似相关。
纯函数，不碰网络和文件；测试见 test_route_policy.py。
"""

CONFIRM = "confirm"
DELIVER = "deliver"
QUEUE = "queue"
REFUSE = "refuse"
DEFER = "defer"


def decide(*, exists=True, cloud=False, archived=False, protected_cwd=False,
           name_conflict=False, has_address=False, address_valid=True, switching=False,
           candidates=1, new_window=None, writer_conflict=False, turn_status="completed",
           pending=0, age_days=None, stale_days=3.0, backend_can_queue=True) -> dict:
    """返回 {decision, reason, allowed_mode, needs_human}。判定顺序固定，先硬拒后确认再排队。"""
    ev = {"writer_conflict": writer_conflict, "turn_status": turn_status,
          "pending": pending, "age_days": age_days, "new_window": new_window}

    if not exists:
        return {"decision": REFUSE, "reason": "no_target", "allowed_mode": None, "needs_human": False, "evidence": ev}
    if cloud:
        return {"decision": REFUSE, "reason": "cloud_no_rollout", "allowed_mode": None, "needs_human": False, "evidence": ev}
    if archived:
        return {"decision": REFUSE, "reason": "archived", "allowed_mode": None, "needs_human": False, "evidence": ev}
    if protected_cwd:
        return {"decision": REFUSE, "reason": "protected_cwd", "allowed_mode": None, "needs_human": False, "evidence": ev}

    # 四类必须确认
    if name_conflict:
        return {"decision": CONFIRM, "reason": "name_conflict_or_binding_conflict", "allowed_mode": None,
                "needs_human": True, "evidence": ev}
    if not has_address and candidates > 1:
        return {"decision": CONFIRM, "reason": "cold_start_multiple_candidates", "allowed_mode": None,
                "needs_human": True, "evidence": ev}
    if has_address and not address_valid and switching:
        return {"decision": CONFIRM, "reason": "address_invalid_need_switch", "allowed_mode": None,
                "needs_human": True, "evidence": ev}
    if new_window == "suspected":
        return {"decision": CONFIRM, "reason": "new_window_suspected_relevant", "allowed_mode": None,
                "needs_human": True, "evidence": ev}

    # 上一轮被中断：队列不会有人来取，排进去也读不到
    if turn_status == "interrupted":
        return {"decision": CONFIRM, "reason": "last_turn_interrupted", "allowed_mode": "sync",
                "needs_human": True, "evidence": ev}

    # 此刻怎样投
    if pending:
        return {"decision": DEFER, "reason": "pending_queue", "allowed_mode": "queue", "needs_human": False, "evidence": ev}
    if writer_conflict or turn_status == "inProgress":
        if backend_can_queue:
            return {"decision": QUEUE, "reason": "writer_conflict_or_turn_running", "allowed_mode": "queue",
                    "needs_human": False, "evidence": ev}
        return {"decision": DEFER, "reason": "busy_no_queue_support", "allowed_mode": None, "needs_human": False, "evidence": ev}

    if age_days is not None and age_days > stale_days:
        return {"decision": CONFIRM, "reason": "over_stale_days", "allowed_mode": "sync", "needs_human": True, "evidence": ev}

    return {"decision": DELIVER, "reason": "clean_and_fresh", "allowed_mode": "sync", "needs_human": False, "evidence": ev}