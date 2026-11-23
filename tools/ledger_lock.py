# -*- coding: utf-8 -*-
r"""账本写入锁（ledger_lock）：防止两个写者互相覆盖。

为什么必须有（2026-11-11 15:06 事故）：
    桌面助手与飞书助手同时写 `工作台\工作台结果.md`。飞书助手那侧用的是"整篇写回"，
    她读到的版本比桌面助手慢，于是用她的版本覆盖了桌面助手的——**115,890 字节一次变成 1,230 字节，112KB 内容没了**。
    两个人都不算错，是方式错了：**一本账不能有两个写者，除非有锁。**

本工具解决三件事：
    1. **独占**：同一时刻只允许一个进程持有写锁（跨进程、跨用户）。
    2. **写前确认**：拿锁时必须报"我要基于哪个版本的字节数/条数改"，版本不符就拒绝。
    3. **写后核对**：写完必须复读，字节数变小超过阈值就自动回滚。

用法（推荐用 `append` 子命令，别自己 open(w)）：
    # 追加一条（安全：自动加锁 + 写前确认 + 写后核对）
    python tools\ledger_lock.py append --file <账本> --text-file <正文文件>

    # 只是想改，自己做前后处理：
    python tools\ledger_lock.py guard --file <账本> --expect-bytes 123164 -- <要执行的命令>

    # 查锁状态 / 强行解锁（只在确认没人写时用）
    python tools\ledger_lock.py status --file <账本>
    python tools\ledger_lock.py release --file <账本> --force

时间戳（2026-11-12 12:1x 加，P0-1）：
    **行首 `[MM-DD HH:MM]` 由程序用 datetime.now() 生成，人不许手写。**
    正文若自带行首时间戳，只校验不采信：超前系统时间 60 秒以上 → 拒收。
    为什么：同一个错我犯了三次（写 00:25 实际 00:20；写 00:55 实际 00:37；
    写 01:31/01:36/01:24 实际 01:12–01:15），每次都是"凭感觉现在几点"。
    靠提醒治不住，只能让机器打点。

退出码：
    0 成功
    3 锁被占用（别人正在写，等一会儿再试）
    4 写前确认失败（版本已变，必须重读）
    5 写后核对失败（已自动回滚）
    6 正文时间戳超前系统时间（你写的是未来时间，把正文里的时间戳删掉重写）
    2 环境问题（文件不存在、参数错）

依据：自拟（本机实测事故驱动）。业界做法参考：SQLite 的 write-ahead lock、
      以及"optimistic concurrency control"（写入时带版本号，不符则拒绝）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# P0-2（2026-11-12 12:5x）：追加/回执都走唯一写文件入口，不自己 open(a)。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_write  # noqa: E402

# 锁文件放在被保护文件旁边，命名 <文件名>.lock
LOCK_SUFFIX = '.lock'
# 锁超过这个秒数视为陈旧（进程崩了但锁没释放）
STALE_SECONDS = 180


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def _lock_path(target: str) -> str:
    return target + LOCK_SUFFIX


def _stat(target: str) -> dict:
    """三样坐标：字节 / 行数 / mtime。"""
    st = os.stat(target)
    with open(target, 'rb') as f:
        lines = f.read().count(b'\n')
    return {
        'bytes': st.st_size,
        'lines': lines,
        'mtime': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
    }


def _read_lock(target: str):
    p = _lock_path(target)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {'corrupt': True}
    try:
        age = time.time() - float(data.get('ts', 0))
    except Exception:
        age = 99999
    data['age'] = age
    data['stale'] = age > STALE_SECONDS
    return data


def cmd_status(args) -> int:
    if not os.path.exists(args.file):
        print('文件不存在：' + args.file)
        return 2
    s = _stat(args.file)
    print('文件：%s' % args.file)
    print('  现值：%d 字节 / %d 行 / mtime %s' % (s['bytes'], s['lines'], s['mtime']))
    lock = _read_lock(args.file)
    if lock is None:
        print('  锁：无（可写）')
        return 0
    if lock.get('corrupt'):
        print('  锁：存在但内容损坏，建议 release --force')
        return 0
    print('  锁：被占用 by %s (pid %s) 于 %s，已 %ds%s'
          % (lock.get('owner'), lock.get('pid'), lock.get('time'),
             int(lock.get('age', 0)), '（陈旧，可用 --force 清）' if lock.get('stale') else ''))
    return 0


def _acquire(args, target: str) -> bool:
    """返回 True 表示拿到锁。"""
    p = _lock_path(target)
    for _ in range(2):  # 陈旧锁允许重试一次
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            lock = _read_lock(target)
            if lock and lock.get('stale'):
                # 陈旧锁：清掉再试
                try:
                    os.remove(p)
                    continue
                except OSError:
                    pass
            print('锁被占用：%s' % json.dumps(lock, ensure_ascii=False))
            print('  正在写的人：%s (pid %s) 于 %s' % (lock.get('owner'), lock.get('pid'), lock.get('time')))
            return False
        payload = json.dumps({
            'owner': args.owner,
            'pid': os.getpid(),
            'time': _now(),
            'ts': time.time(),
        }, ensure_ascii=False)
        os.write(fd, payload.encode('utf-8'))
        os.close(fd)
        return True
    return False


def _release(target: str) -> None:
    p = _lock_path(target)
    try:
        os.remove(p)
    except OSError:
        pass


def cmd_release(args) -> int:
    p = _lock_path(args.file)
    if not os.path.exists(p):
        print('没有锁')
        return 0
    if not args.force:
        lock = _read_lock(args.file)
        if lock and not lock.get('stale'):
            print('锁是新的（%ds），不用 --force 不解。持有者：%s' % (int(lock.get('age', 0)), lock.get('owner')))
            return 3
    _release(args.file)
    print('锁已清除')
    return 0


# 2026-11-11 22:3x 加：账本行必须带"结果词"（内部守则（略））。
# 实测：截至 22:3x，账本 63/63 条都没写 —— 说明这条规矩原来只是"愿望"。
# 按 §F2 的教训（规矩只写在文档里 = 没人读 = 等于没有），改成**在写的时候提醒**。
OUTCOME_WORDS = ('成', '没成', '进行中')

# ---- 时间戳（P0-1，2026-11-12）------------------------------------------------
# 行首 [MM-DD HH:MM] 由程序打；正文自带的只校验不采信。
# 容忍 60 秒：跨进程/跨机时钟可能差几秒，但"我以为是几点"的错至少是几分钟。
FUTURE_TOLERANCE_SECONDS = 60
_TS_HEAD = re.compile(r'^\[(\d{2})-(\d{2}) (\d{2}):(\d{2})\]')
_TS_FULL = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})')


def _parse_head_stamps(body: str, now=None):
    """扫出正文里所有「行首时间戳」。返回 [(行号, 原文片段, 时间, 形状)]。"""
    now = now or datetime.now()
    out = []
    for lineno, line in enumerate(body.splitlines(), 1):
        m = _TS_HEAD.match(line)
        if m:
            mm, dd, hh, mi = (int(x) for x in m.groups())
            try:
                ts = datetime(now.year, mm, dd, hh, mi)
            except ValueError:      # 02-30 之类，交给下面当"解析不了"处理
                out.append((lineno, line.strip()[:48], None, 'head'))
                continue
            out.append((lineno, line.strip()[:48], ts, 'head'))
            continue
        m = _TS_FULL.match(line)
        if m:
            y, mm, dd, hh, mi = (int(x) for x in m.groups())
            try:
                ts = datetime(y, mm, dd, hh, mi)
            except ValueError:
                out.append((lineno, line.strip()[:48], None, 'full'))
                continue
            out.append((lineno, line.strip()[:48], ts, 'full'))
    return out


def _future_stamps(body: str, now=None):
    """返回超前的行首时间戳：[(行号, 片段, 超前秒数)]。

    只查「超前」：把过去写成未来，是这三次事故的共同形状。
    写成更早（补记）不算错——补记本来就该写动作时间。
    """
    now = now or datetime.now()
    bad = []
    for lineno, snippet, ts, _shape in _parse_head_stamps(body, now):
        if ts is None:
            continue
        delta = (ts - now).total_seconds()
        if delta > FUTURE_TOLERANCE_SECONDS:
            bad.append((lineno, snippet, int(delta)))
    return bad


def _stamp(body: str, now=None) -> str:
    """给正文加行首时间戳；正文首行已带时间戳就原样保留（兼容旧写法）。"""
    now = now or datetime.now()
    lines = body.splitlines()
    first = lines[0] if lines else ''
    if _TS_HEAD.match(first) or _TS_FULL.match(first):
        return body
    return '[%s] %s' % (now.strftime('%m-%d %H:%M'), body.lstrip())

# **不能用简单子串判断**：`成` 是常用字，"完成/成本/生成/变成"全会命中，
# 那样等于永远不报警 —— 检查形同虚设（踩坑 §D「写了没核对就报成」的同一个毛病）。
# 只认"独立出现"的结果词：两边是标点/空白/行首行尾。
_OUTCOME_RE = None


def _check_outcome(body: str) -> bool:
    """正文里有没有"独立出现"的结果词。有 → True。"""
    global _OUTCOME_RE
    import re as _re
    if _OUTCOME_RE is None:
        # 多字词（没成／进行中）够独特，出现在哪都算；
        # 单字"成"只认独立出现（否则完成/成本/生成/变成全会误中）。
        # 中文不空格，所以多字词不能要求前边界（"进行中"常紧跟在汉字后面）。
        _OUTCOME_RE = _re.compile(
            r'没成|进行中'
            r'|(?:^|[\s，。；：:、\|｜（）()【】\[\]])成(?:$|[\s，。；：:、\|｜（）()【】\[\]])')
    return bool(_OUTCOME_RE.search(body))


def append_entry(target: str, body: str, owner: str = 'unknown',
                 expect_bytes=None, receipt=None, quiet: bool = False) -> int:
    """给**别的工具**用的记账入口（2026-11-12 12:5x，P0-2 收口）。

    以前 worker_task / comp_check / backup_data 各自 `open(LEDGER,'a')` 手写时间戳 ——
    锁和 P0-1 都绕过了。现在它们应该调这个函数，行为与 CLI 的 append 完全一致：
    校验未来时间(6) → 程序打时间戳 → 独占锁(3) → 写前确认(4) → 追加 → 写后核对(5)。
    返回同样的退出码，调用方自己决定要不要中断。
    """
    def say(msg):
        if not quiet:
            print(msg)

    if not os.path.exists(target):
        say('文件不存在：' + target)
        return 2
    if not body or not body.strip():
        say('正文为空，不写')
        return 2

    future = _future_stamps(body)
    if future:
        say('拒收（退出码 6）：正文里的时间戳超前当前系统时间（现在 %s）' % _now()[:16])
        for lineno, snippet, delta in future:
            say('  第 %d 行：%s —— 比现在晚 %d 秒' % (lineno, snippet, delta))
        return 6

    stamped = _stamp(body)
    if stamped is not body:
        say('已自动打时间戳：%s' % stamped.splitlines()[0][:26])

    if not _acquire(_NS(owner), target):
        return 3

    try:
        before = _stat(target)
        if expect_bytes is not None and before['bytes'] != expect_bytes:
            say('写前确认失败：期望 %d 字节，实际 %d 字节' % (expect_bytes, before['bytes']))
            return 4

        line = ('\n' if not stamped.startswith('\n') else '') + stamped.rstrip() + '\n'
        # 追加也走 safe_write：同一套闸（拒控制字符、LF 归一），不整篇重写。
        try:
            safe_write.append_text(target, line)
        except safe_write.SafeWriteError as exc:
            # 控制字符等被闸拦下 → 原样把它的退出码（3/2）报出去，不写成成功
            say(exc.message)
            return exc.code

        after = _stat(target)
        if after['bytes'] < before['bytes']:
            say('写后核对失败：%d -> %d 字节（变小了！）' % (before['bytes'], after['bytes']))
            return 5
        say('已追加：%d -> %d 字节（+%d）/ %d -> %d 行'
            % (before['bytes'], after['bytes'], after['bytes'] - before['bytes'],
               before['lines'], after['lines']))
        if not _check_outcome(body):
            say('  ⚠ 这条账没写结果词（成／没成／进行中）—— 内部守则（略）')
        if receipt:
            rec = {'at': _now(), 'owner': owner, 'file': target,
                   'before': before, 'after': after}
            try:
                safe_write.append_text(receipt, json.dumps(rec, ensure_ascii=False) + '\n')
            except Exception:
                pass
        return 0
    finally:
        _release(target)


class _NS:
    """冒名顶替 argparse.Namespace（_acquire 只需要 .owner）。"""

    def __init__(self, owner):
        self.owner = owner


def cmd_append(args) -> int:
    if not os.path.exists(args.text_file):
        print('正文文件不存在：' + args.text_file)
        return 2
    try:
        body = open(args.text_file, encoding='utf-8-sig').read()
    except OSError as exc:
        print('正文文件读不了：%s（%s）' % (args.text_file, exc))
        return 2
    return append_entry(args.file, body, owner=args.owner,
                        expect_bytes=args.expect_bytes, receipt=args.receipt)


def cmd_guard(args) -> int:
    """拿锁 -> 校验版本 -> 执行命令 -> 核对 -> 放锁。"""
    target = args.file
    if not os.path.exists(target):
        print('文件不存在：' + target)
        return 2
    if not args.cmd:
        print('guard 需要在 -- 之后给一条命令')
        return 2
    if not _acquire(args, target):
        return 3
    try:
        before = _stat(target)
        if args.expect_bytes is not None and before['bytes'] != args.expect_bytes:
            print('写前确认失败：期望 %d，实际 %d' % (args.expect_bytes, before['bytes']))
            return 4
        print('持锁执行：%s' % ' '.join(args.cmd))
        rc = subprocess.call(args.cmd)
        after = _stat(target)
        print('执行后：%d -> %d 字节（%+d）' % (before['bytes'], after['bytes'],
                                              after['bytes'] - before['bytes']))
        if after['bytes'] < before['bytes']:
            print('警告：文件变小了。如果这不是预期（例如切档），请立刻从备份恢复。')
            return 5
        return 0 if rc == 0 else 1
    finally:
        _release(target)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    ap = argparse.ArgumentParser(description='账本写入锁：独占 / 写前确认 / 写后核对')
    ap.add_argument('--owner', default=os.environ.get('LEDGER_OWNER', 'unknown'),
                    help='谁在写（desktop_bot / feishu_bot / user），记进锁文件便于排查')
    sub = ap.add_subparsers(dest='action', required=True)

    p = sub.add_parser('status', help='看现值与锁状态')
    p.add_argument('--file', required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('release', help='清锁（--force 可清新锁）')
    p.add_argument('--file', required=True)
    p.add_argument('--force', action='store_true')
    p.set_defaults(func=cmd_release)

    p = sub.add_parser('append', help='安全追加一条（推荐）')
    p.add_argument('--file', required=True)
    p.add_argument('--text-file', required=True, help='正文文件（UTF-8）')
    p.add_argument('--expect-bytes', type=int, default=None, help='写前确认：期望当前字节数')
    p.add_argument('--receipt', default=None, help='把本次写入的 before/after 追加到这个 jsonl')
    p.set_defaults(func=cmd_append)

    p = sub.add_parser('guard', help='持锁执行一条命令')
    p.add_argument('--file', required=True)
    p.add_argument('--expect-bytes', type=int, default=None)
    p.add_argument('cmd', nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_guard)

    args = ap.parse_args(argv)
    if args.action == 'guard' and args.cmd and args.cmd[0] == '--':
        args.cmd = args.cmd[1:]
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
