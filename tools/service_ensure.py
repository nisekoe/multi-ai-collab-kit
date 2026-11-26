#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机轻量核心服务统一自愈底座（service_ensure.py v2）。

核心设计：
1. 轻量核心常驻服务掉线时，工具自动拉起，不停下来问人；只有不可逆操作才问人。
2. 严禁在 AI 命令行进程树内直接 Popen 桌面 GUI 服务；统一走宿主机交互式计划任务（schtasks /run）。
3. 严格健康判据：对 requires_interactive_desktop=true 的服务，必须显式校验
   service 身份、desktop_ok=True、tk_ready=True、ok=True；缺字段/旧版无健康端点/HTTP活但Tk未就绪一律判不健康。
4. 跨进程文件锁按 task_name 聚合在共享固定目录；未拿到锁的并发调用严禁重复触发调度。
5. 全程共用单调时钟总截止时间（deadline），限制响应体大小与每段超时；配置/锁异常均返回非零与可读中文原因。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import locale
import math
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import paths  # noqa: E402

DEFAULT_SERVICES = {
    "card_service": {
        "label": "系统选窗卡片服务",
        "host": "127.0.0.1",
        "port": 8790,
        "health_url": "http://127.0.0.1:8790/api/health",
        "fallback_url": "http://127.0.0.1:8790/api/pending",
        "task_name": "MultiAICollab-CardService",
        "requires_interactive_desktop": True,
        "wait_seconds": 8.0,
    }
}
TEMPLATE_REGISTRY = HERE.parent / "templates" / "services.json"
FALLBACK_STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / ".multi-ai-collab-kit"
MAX_HEALTH_BYTES = 65536

_OPENER = build_opener(ProxyHandler({}))


def _local_dir(sub: str) -> Path:
    try:
        return paths.resolve(f"_local/{sub}", for_write=False)
    except paths.RootError:
        return FALLBACK_STATE_DIR / sub


def _log(msg: str) -> None:
    try:
        log_path = _local_dir("logs") / "service_ensure.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def registry_path() -> Path | None:
    """解析服务登记表路径：COLLAB_SERVICES_FILE ＞ 工作根 _state/services.json ＞ 包内模板。"""
    override = (os.environ.get("COLLAB_SERVICES_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    try:
        candidate = paths.resolve("_state/services.json", for_write=False)
        if candidate.is_file():
            return candidate
    except paths.RootError:
        pass
    if TEMPLATE_REGISTRY.is_file():
        return TEMPLATE_REGISTRY
    return None


def load_registry() -> dict[str, dict]:
    p = registry_path()
    if p is not None and p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise ValueError(f"服务登记表 JSON 解析失败（{p}）：{exc}") from exc
        svcs = data.get("services") if isinstance(data, dict) else None
        if not isinstance(svcs, dict) or not svcs:
            raise ValueError(f"服务登记表为空或格式无效：{p}")
        return dict(svcs)
    if (os.environ.get("COLLAB_SERVICES_FILE") or "").strip():
        raise FileNotFoundError(f"服务登记表不存在：{p}")
    return {k: dict(v) for k, v in DEFAULT_SERVICES.items()}


def get_service_cfg(name: str) -> dict:
    svcs = load_registry()
    if name not in svcs or not isinstance(svcs[name], dict):
        p = registry_path()
        raise KeyError(f"未在 {p or 'services.json'} 登记的服务：{name}")
    cfg = dict(svcs[name])
    if name == "card_service":
        port = paths.card_port()
        cfg["port"] = port
        cfg["health_url"] = f"http://127.0.0.1:{port}/api/health"
        cfg["fallback_url"] = f"http://127.0.0.1:{port}/api/pending"
        env_task = (os.environ.get("CARD_SERVICE_TASK") or "").strip()
        if env_task:
            cfg["task_name"] = env_task
    return cfg


def _lock_key(name: str, cfg: dict) -> str:
    raw = str(cfg.get("task_name") or name).strip() or name
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)


@contextlib.contextmanager
def _service_lock(lock_key: str, deadline: float):
    lock_dir = _local_dir("locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f"ensure_{lock_key}.lock"
    fh = open(lock_file, "a+b")
    locked = False
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                rem = _remaining(deadline)
                if rem <= 0:
                    break
                time.sleep(min(0.15, rem))
        yield locked
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _http_get_json(url: str, deadline: float) -> tuple[int, dict | list | None, str]:
    rem = _remaining(deadline)
    if rem <= 0:
        return 0, None, "timeout"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=min(2.0, rem)) as resp:
            code = getattr(resp, "status", 200)
            chunks = []
            total = 0
            while total < MAX_HEALTH_BYTES:
                rem_read = _remaining(deadline)
                if rem_read <= 0:
                    return 0, None, "timeout"
                raw_sock = getattr(getattr(resp, "fp", None), "raw", None)
                sock = getattr(raw_sock, "_sock", None)
                if sock is not None and hasattr(sock, "settimeout"):
                    try:
                        sock.settimeout(min(2.0, rem_read))
                    except OSError:
                        pass
                chunk = resp.read(min(4096, MAX_HEALTH_BYTES - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if _remaining(deadline) <= 0:
                return 0, None, "timeout"
            raw = b"".join(chunks)
            try:
                return code, json.loads(raw.decode("utf-8", errors="replace")), ""
            except ValueError:
                return code, None, "invalid_json"
    except HTTPError as exc:
        code = exc.code
        exc.close()
        return code, None, f"http_{code}"
    except Exception as exc:
        return 0, None, f"unreachable:{exc}"


def _tcp_open(host: str, port: int, deadline: float) -> bool:
    rem = _remaining(deadline)
    if rem <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=min(1.5, rem)):
            return True
    except OSError:
        return False


def check(name: str, timeout: float = 2.0) -> tuple[bool, str]:
    """严格探活：返回 (healthy, detail)。"""
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        return False, "invalid_timeout"
    deadline = time.monotonic() + float(timeout)
    try:
        cfg = get_service_cfg(name)
    except Exception as exc:
        return False, str(exc)

    host = str(cfg.get("host") or "127.0.0.1")
    port = int(cfg.get("port") or 0)
    health_url = str(cfg.get("health_url") or "")
    fallback_url = str(cfg.get("fallback_url") or "")
    need_desktop = bool(cfg.get("requires_interactive_desktop"))

    if health_url:
        code, data, err = _http_get_json(health_url, deadline)
        if 200 <= code < 300:
            if need_desktop:
                if not isinstance(data, dict):
                    return False, f"wrong_service_or_missing_fields({err or 'non_dict'})"
                if data.get("service") != name:
                    return False, f"wrong_service_or_missing_fields(service={data.get('service')!r})"
                if data.get("desktop_ok") is not True:
                    return False, (
                        f"non_interactive_desktop_instance(pid={data.get('pid')},"
                        f"winstation={data.get('winstation')},desktop={data.get('desktop')},"
                        f"session={data.get('session_id')})"
                    )
                if data.get("tk_ready") is not True or data.get("ok") is not True:
                    return False, (
                        f"tk_not_ready(pid={data.get('pid')},tk_ready={data.get('tk_ready')},"
                        f"hb_age={data.get('tk_heartbeat_age_s')},tk_error={data.get('tk_error')})"
                    )
                return True, "healthy"
            return True, "healthy"
        if code == 404 and fallback_url and _remaining(deadline) > 0:
            fb_code, _, _ = _http_get_json(fallback_url, deadline)
            if 200 <= fb_code < 300:
                if need_desktop:
                    return False, "legacy_instance_no_health"
                return True, "healthy_legacy"
        return False, err or f"http_{code}"

    if port and _tcp_open(host, port, deadline):
        return True, "tcp_open"
    return False, "port_closed"


def _run_scheduled_task(task_name: str, deadline: float) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "non_windows_no_schtasks"
    rem = _remaining(deadline)
    if rem <= 0:
        return False, "budget_exhausted_before_schtasks"
    enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        cp = subprocess.run(
            ["schtasks", "/run", "/tn", task_name],
            capture_output=True,
            text=True,
            encoding=enc,
            errors="replace",
            timeout=min(10.0, rem),
        )
    except Exception as exc:
        return False, f"schtasks_exec_error:{exc}"
    if cp.returncode == 0:
        return True, (cp.stdout or "").strip() or "triggered"
    err = (cp.stderr or cp.stdout or "").strip().replace("\r\n", " ").replace("\n", " ")[:240]
    return False, f"schtasks_rc={cp.returncode}:{err}"


def register_card_task(task_name: str | None = None) -> tuple[bool, str]:
    """在 Windows 上注册当前用户交互式登录自启的卡片服务计划任务（普通权限）。"""
    if os.name != "nt":
        return False, "non_windows_no_schtasks"
    if not task_name:
        try:
            task_name = str(get_service_cfg("card_service").get("task_name") or "MultiAICollab-CardService")
        except Exception:
            task_name = "MultiAICollab-CardService"
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.is_file() else Path(sys.executable))
    svc = str((HERE / "card_service.py").resolve())
    tr = f'"{exe}" "{svc}"'
    cmd = [
        "schtasks", "/create",
        "/tn", task_name,
        "/tr", tr,
        "/sc", "ONLOGON",
        "/rl", "LIMITED",
        "/it",
        "/f",
    ]
    enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, encoding=enc, errors="replace", timeout=15)
    except Exception as exc:
        return False, f"schtasks_create_error:{exc}"
    if cp.returncode == 0:
        return True, (cp.stdout or "").strip() or f"registered:{task_name}"
    err = (cp.stderr or cp.stdout or "").strip().replace("\r\n", " ").replace("\n", " ")[:240]
    return False, f"schtasks_create_rc={cp.returncode}:{err}"


def ensure(name: str, wait: float | None = None) -> tuple[bool, str]:
    """确保登记的轻量服务处于真实可用状态；不通则通过宿主机计划任务自动拉起。"""
    try:
        cfg = get_service_cfg(name)
    except Exception as exc:
        return False, str(exc)

    wait_val = cfg.get("wait_seconds", 8.0) if wait is None else wait
    if not isinstance(wait_val, (int, float)) or not math.isfinite(wait_val) or wait_val <= 0:
        return False, f"无效等待时间：{wait_val!r}（必须为有限正数）"
    wait_s = float(wait_val)
    deadline = time.monotonic() + wait_s

    ok, detail = check(name, timeout=min(1.5, _remaining(deadline) or 0.1))
    if ok:
        return True, "already_running"

    task_name = str(cfg.get("task_name") or "").strip()
    label = str(cfg.get("label") or name)
    lkey = _lock_key(name, cfg)

    try:
        with _service_lock(lkey, deadline) as locked:
            if not locked:
                rem = _remaining(deadline)
                if rem > 0.05:
                    ok_after, det_after = check(name, timeout=min(1.0, rem))
                    if ok_after:
                        return True, "started_by_other_caller"
                    detail = det_after
                return False, f"{label}正由另一调用拉起，本次等待锁超时（未重复触发调度，当前状态：{detail}）"

            # 持锁后二次探活，避免并发重复拉起
            rem = _remaining(deadline)
            if rem <= 0:
                return False, f"{label}等待超时（{detail}）"
            ok2, detail2 = check(name, timeout=min(1.5, rem))
            if ok2:
                return True, "already_running"

            if detail2.startswith(("non_interactive_desktop_instance", "tk_not_ready", "legacy_instance_no_health", "wrong_service_or_missing_fields")):
                _log(f"[{name}] unhealthy listener occupying port: {detail2}")
                return False, (
                    f"{label}端口当前被异常/旧版/非交互式实例占用（{detail2}），"
                    f"无法保证真实桌面弹窗；请先清理占用进程后由宿主机任务 {task_name} 拉起"
                )

            if not task_name:
                return False, f"{label}未运行，且未配置宿主机计划任务（detail={detail2}）"

            _log(f"[{name}] down ({detail2}) -> triggering scheduled task {task_name}")
            trig_ok, trig_detail = _run_scheduled_task(task_name, deadline)
            if not trig_ok:
                _log(f"[{name}] schtasks failed: {trig_detail}")
                return False, f"{label}未运行，尝试通过宿主机计划任务 {task_name} 自动拉起失败（{trig_detail}）"

            last_detail = "starting"
            while _remaining(deadline) > 0:
                time.sleep(min(0.4, _remaining(deadline)))
                rem_c = _remaining(deadline)
                if rem_c <= 0:
                    break
                h_ok, last_detail = check(name, timeout=min(1.5, rem_c))
                if h_ok:
                    _log(f"[{name}] started via {task_name} ({last_detail})")
                    return True, f"started_via_task:{task_name}"
                if last_detail.startswith(("non_interactive_desktop_instance", "wrong_service_or_missing_fields", "legacy_instance_no_health")):
                    break

            _log(f"[{name}] timeout waiting for ready: {last_detail}")
            return False, (
                f"{label}未运行，已触发宿主机计划任务 {task_name} 但 {wait_s:.1f} 秒总预算内仍未就绪（{last_detail}）"
            )
    except OSError as exc:
        return False, f"{label}自愈锁异常（{_local_dir('locks')}）：{exc}"


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="本机轻量核心服务统一自愈工具")
    ap.add_argument("service", nargs="?", default="card_service", help="服务名（见 _state/services.json）")
    ap.add_argument("--check", action="store_true", help="只探活，不拉起")
    ap.add_argument("--list", action="store_true", help="列出已登记服务及其状态")
    ap.add_argument("--register-card-task", action="store_true", help="在 Windows 注册交互式普通权限卡片计划任务")
    ap.add_argument("--wait", type=float, default=None, help="总等待预算秒数（有限正数）")
    args = ap.parse_args(argv)

    if args.register_card_task:
        ok, detail = register_card_task()
        print(json.dumps({"action": "register_card_task", "ok": ok, "detail": detail}, ensure_ascii=False))
        return 0 if ok else 1

    if args.list:
        try:
            reg = load_registry()
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        out = {}
        for k, v in reg.items():
            ok, detail = check(k, timeout=1.5)
            out[k] = {"label": v.get("label", k), "port": v.get("port"), "task": v.get("task_name"), "ok": ok, "detail": detail}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if all(x["ok"] for x in out.values()) else 1

    ok, detail = check(args.service) if args.check else ensure(args.service, wait=args.wait)
    print(json.dumps({"service": args.service, "ok": ok, "detail": detail}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
