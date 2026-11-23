import os
# -*- coding: utf-8 -*-
"""集成验收：选中地址＋工作区授权流到实际发送入口 reach_gpt（只读 dry-run，零真投零额度）。"""
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import picker, window_picker as wp

print("=== 选中地址 → 发送入口 授权链集成验收（dry-run）===")
cands = picker.gather()
codex_all = [c for c in cands if c.leg == "codex"]
normal = [c for c in codex_all if c.reachable]
edu = [c for c in codex_all if "protected-workspace" in (c.cwd or "") or "protected-workspace" in (c.title or "")]
print("codex 候选%d，合格%d，受保护%d" % (len(codex_all), len(normal), len(edu)))

if normal:
    c = normal[0]
    code, info = picker.dispatch(c, "integration", dry_run=True)
    print("dispatch 复验：exit=%d verified=%s" % (code, info.get("verified")))
    ccode, cinfo = picker.commit_codex(c, "integration-test", dry_run=True)
    print("commit_codex 发送入口 dry-run：exit=%d ok=%s（应0，授权链通）" % (ccode, cinfo.get("ok")))
    assert ccode == 0, "正常窗发送入口应放行"

if edu:
    e = edu[0]
    ecode, einfo = picker.commit_codex(e, "integration-test", dry_run=True)
    print("受保护窗 %s 发送入口 dry-run：exit=%d（应6 workspace_protected，授权流到入口）" % (e.id[:8], ecode))
    assert ecode == 6, "受保护窗发送入口应被保护闸拦"
    assert einfo.get("reason") == "workspace_protected" or "workspace_protected" in einfo.get("output", ""), "应报 workspace_protected（commit 复验或 reach_gpt 入口）"
else:
    print("（raw gather 无受保护 codex 候选——enumerate 已保护排除，跳过活体保护窗直验）")

# TOCTOU：选窗时工作区作为 --expected-cwd 传发送入口最终复验（GPT 复审：父进程预检后到读库间漂移）
import subprocess
reach = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reach_gpt.py")
if normal:
    nid = normal[0].id
    p = subprocess.run([sys.executable, reach, "--thread", nid, "--text", "t", "--expected-cwd", "F:/wrong/path", "--dry-run"], capture_output=True, text=True, encoding="utf-8", timeout=120)
    print("TOCTOU 错 expected-cwd 发送入口：exit=%d（应6 漂移）" % p.returncode)
    assert p.returncode == 6, "选窗时工作区不符发送入口应拒"
    assert "工作区已变更" in (p.stdout + p.stderr)

print("=== 集成验收 PASS：选中地址＋共享工作区授权＋选窗时 cwd 最终复验确实流到实际发送入口 ===")
