"""显式开启的宿主只读集成测试。"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    if os.environ.get("MACK_HOST_TESTS") != "1":
        print("跳过宿主集成。需要时设置 MACK_HOST_TESTS=1。")
        return 0

    import _testenv
    _testenv.sandbox()
    import picker

    # 只枚举本地 Codex，避免为了测试这一条线路查询其他宿主服务。
    cands, _complete, _detail = picker.enumerate_codex()
    print("=== 选中地址 → 发送入口 授权链集成验收（dry-run）===")
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

    # 模拟选定后目录漂移，入口必须在提交前拦住。
    if normal:
        with tempfile.TemporaryDirectory() as temp:
            body = Path(temp) / "probe.txt"
            body.write_text("integration", encoding="utf-8")
            entry = Path(__file__).resolve().parents[1] / "tools" / "send_codex.py"
            result = subprocess.run(
                [sys.executable, str(entry), "--thread", normal[0].id, "--confirmed",
                 "--file", str(body), "--expected-cwd", temp, "--dry-run"],
                capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
        assert result.returncode == 6, result.stdout + result.stderr
        assert "cwd_drift" in result.stdout, result.stdout

    print("=== 集成验收 PASS（存在的目标已复验，无真实投递）===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
