import os
# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import picker
import window_picker as wp
from unittest import mock

n = 0
def ok(cond, label):
    global n
    assert cond, "FAIL: " + label
    n += 1
    print("  OK ", label)

print("=== normalization ===")
row = dict(leg="codex", hostId="local", id="t1", alias="a", display_title="T", title="a",
           role="desktop", cwd="F:/x", status="idle", lastActivity="", busySecondsAgo=None,
           reachable=True, unusableReason=None, truncated=False, meta={})
c = picker._core_from_dict(row)
ok(isinstance(c, wp.UnifiedCandidate) and c.key == ("codex", "local", "t1"), "dict -> core candidate")

print("=== protected workspace policy ===")
ok(not picker._workspace_allowed(("codex", "local", "t1"), "X:/protected-workspace/y"), "protected cwd blocked")
ok(not picker._workspace_allowed(("codex", "local", "t1"), "file:///X:/protected%2Dworkspace/y"), "protected URL-encoded blocked")
ok(picker._workspace_allowed(("codex", "local", "t1"), "X:/other/y"), "non-protected allowed")

print("=== gather dedup (mocked legs) ===")
cp = wp.UnifiedCandidate(leg="porta", hostId="antigravity", id="conv1", title="P")
cc = wp.UnifiedCandidate(leg="codex", hostId="local", id="t1", title="C")
with mock.patch.object(picker, "enumerate_codex", return_value=([cc, cc], False, "")), \
     mock.patch.object(picker, "enumerate_porta", return_value=([cp], False, "", "antigravity")), \
     mock.patch.object(picker, "enumerate_harbor", return_value=([], False, "deferred")):
    g = picker.gather()
    ok(len(g) == 2, "gather dedups by (leg,hostId,id)")
    code, payload = picker.pick(keyword="P")
    ok(code == wp.EXIT_OK and payload.id == "conv1", "pick keyword through orchestrator")

print("=== dispatch routing (dry_run, mocked porta) ===")
ccx = wp.UnifiedCandidate(leg="codex", hostId="local", id="t1", title="C", cwd="F:/x")
keyx = ("codex", "local", "t1")
snap_ok = mock.Mock(threads={keyx: {"cwd": "F:/x"}})
with mock.patch("codex_picker_adapter.read_snapshot", return_value=snap_ok), \
     mock.patch("codex_picker_adapter.verify_target", return_value=mock.Mock(reason=None)):
    code, info = picker.dispatch(ccx, "hi", dry_run=True)
    ok(code == wp.EXIT_OK and info.get("verified") and "--thread" in info["route"], "codex re-verify pass (trusted host + cwd match) -> route")
with mock.patch("codex_picker_adapter.read_snapshot", return_value=snap_ok), \
     mock.patch("codex_picker_adapter.verify_target", return_value=mock.Mock(reason="archived")):
    code, info = picker.dispatch(ccx, "hi", dry_run=True)
    ok(code == wp.EXIT_UNQUALIFIED and info["reason"] == "archived", "codex re-verify archived -> exit6")
cc_host = wp.UnifiedCandidate(leg="codex", hostId="evil", id="t1", cwd="F:/x")
with mock.patch("codex_picker_adapter.read_snapshot", return_value=snap_ok) as mrs, \
     mock.patch("codex_picker_adapter.verify_target", return_value=mock.Mock(reason="host_mismatch")):
    code, info = picker.dispatch(cc_host, "hi", dry_run=True)
    ok(code == wp.EXIT_UNQUALIFIED and info["reason"] == "host_mismatch", "codex 异 host -> host_mismatch exit6")
    ok(mrs.call_args.kwargs.get("host_id") == picker.LOCAL_HOST_ID, "snapshot read by trusted local host")
snap_drift = mock.Mock(threads={keyx: {"cwd": "F:/changed"}})
with mock.patch("codex_picker_adapter.read_snapshot", return_value=snap_drift), \
     mock.patch("codex_picker_adapter.verify_target", return_value=mock.Mock(reason=None)):
    code, info = picker.dispatch(ccx, "hi", dry_run=True)
    ok(code == wp.EXIT_UNQUALIFIED and info["reason"] == "cwd_drift", "codex 选中后 cwd 漂移 -> cwd_drift exit6")
# commit_codex 共用 fs_path_key：raw a%20b 与 a b 是不同目录，提交前复验应拒（GPT m-1358）
cc_pct = wp.UnifiedCandidate(leg="codex", hostId="local", id="t1", cwd="F:/a%20b")
snap_pct = mock.Mock(threads={keyx: {"cwd": "F:/a b"}})
with mock.patch("codex_picker_adapter.read_snapshot", return_value=snap_pct), \
     mock.patch("codex_picker_adapter.verify_target", return_value=mock.Mock(reason=None)):
    code, info = picker.commit_codex(cc_pct, "hi", dry_run=True)
    ok(code == wp.EXIT_UNQUALIFIED and info["reason"] == "cwd_drift", "commit_codex raw a%20b vs a b 判不同目录 -> cwd_drift exit6")
import codex_picker_adapter as cxa
print("=== fs_path_key 文件系统路径比对（GPT 复审）===")
ok(cxa.fs_path_key("F:/Proj/X") == cxa.fs_path_key("f:\\proj\\x"), "fs case-insensitive eq")
ok(cxa.fs_path_key("F:/a/b") == cxa.fs_path_key("F:\\a\\b"), "fs separator eq")
ok(cxa.fs_path_key("F:/a/") == cxa.fs_path_key("F:/a"), "fs trailing sep eq")
ok(cxa.fs_path_key("F:/a/./b") == cxa.fs_path_key("F:/a/b"), "fs dot resolve eq")
ok(cxa.fs_path_key("F:/a  b") != cxa.fs_path_key("F:/a b"), "fs internal whitespace count distinct (不同目录)")
ok(cxa.fs_path_key("F:/a b") != cxa.fs_path_key("F:/a c"), "fs cross-entry path change distinct")
ok(cxa.fs_path_key("F:/a%20b") != cxa.fs_path_key("F:/a b"), "fs raw percent literal distinct (a%20b 非 a b)")
ok(cxa.fs_path_key("file:///F:/a%20b") == cxa.fs_path_key("F:/a b"), "fs file:// URI decode eq")
ok(not cxa.default_workspace_allowed(("codex", "local", "t1"), "X:/protected-workspace/y"), "shared policy blocks protected workspace")
ok(picker._workspace_allowed(("codex", "local", "t1"), "X:/protected-workspace/y") == cxa.default_workspace_allowed(("codex", "local", "t1"), "X:/protected-workspace/y"), "picker 与 adapter 共享同一工作区授权")
code, info = picker.dispatch(wp.UnifiedCandidate(leg="harbor", hostId="h", id="s1"), "hi", dry_run=True)
ok(info["leg"] == "harbor" and "--session" in info["route"], "harbor routes to reach_dsh --session")
with mock.patch("porta_adapter.PortaAdapter.deliver", return_value=(0, {"ok": True, "dry_run": True})) as md:
    code, info = picker.dispatch(cp, "hi", peer_agent="desktop_bot", dry_run=True)
    ok(code == 0 and md.call_args.args[0] == "conv1", "porta dispatch calls adapter.deliver(conv_id)")
    ok(md.call_args.kwargs.get("dry_run") is True, "porta dry_run forwarded")
code, info = picker.dispatch(wp.UnifiedCandidate(leg="mars", hostId="h", id="x"), "hi")
ok(code == wp.EXIT_ENV and info["reason"] == "unknown_leg", "unknown leg -> env error")

print("\n全过：%d 项断言" % n)
