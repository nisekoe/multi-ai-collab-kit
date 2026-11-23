import os as _os, sys as _sys
_TOOLS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)
"""Offline CLI tests using a temporary installation and version record."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
import zipfile

import kit_update


MANIFEST_URL = "https://updates.example.test/kit-manifest.json"
ASSET_URL = "https://updates.example.test/kit-1.10.0.zip"
TODAY = "2026-11-24T11:30:00+00:00"
SMOKE_SCRIPT = "import argparse\nargparse.ArgumentParser().parse_args()\n"


class KitUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.install = self.root / "installed"
        self.install.mkdir()
        self.marker = self.install / "payload.txt"
        self.marker.write_text("original", encoding="utf-8")
        self.record = self.root / "kit-version.json"
        self.record.write_text(json.dumps({"version": "1.9.0"}), encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.object(kit_update, "now", return_value=TODAY).start()
        self.requests = patch.object(kit_update, "open_url").start()

    def release(self, version="1.10.0", minimum="0.4.0", bad_digest=False, files=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("release/tools/init_rules.py", SMOKE_SCRIPT)
            archive.writestr("release/payload.txt", "replacement")
            for name, content in (files or {}).items():
                archive.writestr(name, content)
        bundle = buffer.getvalue()
        digest = "0" * 64 if bad_digest else hashlib.sha256(bundle).hexdigest()
        manifest = json.dumps({
            "kit": kit_update.KIT,
            "version": version,
            "minDsh": minimum,
            "assets": [{"name": f"kit-{version}.zip", "sha256": digest, "size": len(bundle)}],
        }).encode("utf-8")
        responses = {
            MANIFEST_URL: manifest,
            f"https://updates.example.test/kit-{version}.zip": bundle,
        }
        self.requests.side_effect = lambda url: io.BytesIO(responses[url])

    def run_cli(self, *args):
        command = [
            "--install-dir", str(self.install),
            "--state-file", str(self.record),
            "--manifest-url", MANIFEST_URL,
            *args,
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = kit_update.main(command)
        return result, stdout.getvalue(), stderr.getvalue()

    def state(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def assert_original(self):
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.state()["version"], "1.9.0")
        self.assertFalse(list(self.root.glob(".kit-stage-*")))
        self.assertFalse(list(self.root.glob(".kit-backup-*")))

    def test_same_version_is_not_installed(self):
        self.release(version="1.9.0")
        code, output, errors = self.run_cli("--apply", "--dsh-version", "0.4.0", "--yes")
        self.assertEqual(code, 0, errors)
        self.assertIn("No update", output)
        self.requests.assert_called_once_with(MANIFEST_URL)
        self.assert_original()

    def test_higher_version_is_reported_without_installing(self):
        self.release()
        code, output, errors = self.run_cli("--check", "--dsh-version", "0.4.0")
        self.assertEqual(code, 0, errors)
        self.assertIn("Update available to 1.10.0", output)
        self.assertEqual(self.state()["lastCheck"]["at"], TODAY)
        self.requests.assert_called_once_with(MANIFEST_URL)
        self.assert_original()

    def test_sha256_mismatch_preserves_installed_version(self):
        self.release(bad_digest=True)
        code, _, errors = self.run_cli("--apply", "--dsh-version", "0.4.0", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("SHA-256", errors)
        self.assertIn("SHA-256", self.state()["lastError"])
        self.assert_original()

    def test_min_dsh_rejects_older_pre_release_without_downloading_zip(self):
        self.release()
        code, _, errors = self.run_cli("--apply", "--dsh-version", "0.4.0-rc.1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("requires DSH >= 0.4.0", errors)
        self.requests.assert_called_once_with(MANIFEST_URL)
        self.assert_original()

    def test_apply_uses_cached_check_and_rollback_restores_directory_and_record(self):
        self.release()
        self.assertEqual(self.run_cli("--check")[0], 0)
        code, output, errors = self.run_cli("--apply", "--dsh-version", "0.4.0", "--yes")
        self.assertEqual(code, 0, errors)
        self.assertIn("Restart DSH", output)
        self.assertEqual(self.state()["version"], "1.10.0")
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "replacement")
        self.assertEqual(self.state()["previous"]["version"], "1.9.0")
        self.assertEqual([call.args[0] for call in self.requests.call_args_list], [MANIFEST_URL, ASSET_URL])

        code, output, errors = self.run_cli("--rollback", "--yes")
        self.assertEqual(code, 0, errors)
        self.assertIn("Restored kit 1.9.0", output)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "original")
        self.assertFalse((self.install / "tools").exists())
        self.assertEqual(len(list(self.root.glob(".kit-backup-*"))), 1)
        code, output, errors = self.run_cli("--status")
        self.assertEqual(code, 0, errors)
        self.assertIn("Installed version 1.9.0", output)
        self.assertIn(TODAY, output)
        self.assertEqual(self.requests.call_count, 2)

    def test_offline_check_is_quiet_and_attempted_once_per_day(self):
        self.requests.side_effect = URLError("offline")
        self.assertEqual(self.run_cli("--check"), (0, "", ""))
        self.assertEqual(self.run_cli("--check"), (0, "", ""))
        self.requests.assert_called_once_with(MANIFEST_URL)
        self.assertIn("offline", self.state()["lastError"])
        self.assertEqual(self.state()["lastCheck"]["at"], TODAY)
        self.assert_original()

    def test_existing_installation_can_register_its_old_version_when_upgrading(self):
        self.record.unlink()
        self.release()
        code, _, errors = self.run_cli(
            "--apply", "--dsh-version", "0.4.0", "--installed-version", "1.9.0", "--yes",
        )
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.state()["previous"]["version"], "1.9.0")
        self.assertEqual(self.run_cli("--rollback", "--yes")[0], 0)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.state()["version"], "1.9.0")

    def test_second_writer_is_refused_while_state_is_locked(self):
        with kit_update.lock(self.record.with_name(self.record.name + ".lock")):
            code, _, errors = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("Cannot lock", errors)
        self.requests.assert_not_called()
        self.assert_original()

    def test_default_confirmation_allows_declining_before_download(self):
        self.release()
        with patch("builtins.input", return_value="n") as prompt:
            code, output, errors = self.run_cli("--apply", "--dsh-version", "0.4.0")
        self.assertEqual(code, 0, errors)
        prompt.assert_called_once()
        self.assertIn("cancelled", output)
        self.requests.assert_called_once_with(MANIFEST_URL)
        self.assert_original()

    def test_archive_path_traversal_is_rejected(self):
        self.release(files={"../escape.txt": "must stay out"})
        code, _, errors = self.run_cli("--apply", "--dsh-version", "0.4.0", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("unsafe path", errors)
        self.assertFalse(list(self.root.rglob("escape.txt")))
        self.assert_original()

    def test_failed_self_check_preserves_installed_version(self):
        self.release(files={"release/tools/kit_update.py": "raise SystemExit(3)\n"})
        code, _, errors = self.run_cli("--apply", "--dsh-version", "0.4.0", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("self-check failed", errors)
        self.assert_original()

    def test_failed_candidate_rename_restores_old_directory(self):
        self.release()
        real_rename = Path.rename

        def fail_candidate_move(source, destination):
            if source.name == "candidate" and destination == self.install:
                raise PermissionError("simulated rename failure")
            return real_rename(source, destination)

        with patch.object(Path, "rename", fail_candidate_move):
            code, _, errors = self.run_cli("--apply", "--dsh-version", "0.4.0", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("simulated rename failure", self.state()["lastError"])
        self.assertIsNone(self.state()["pending"])
        self.assert_original()

    def test_failed_version_record_write_restores_old_directory(self):
        self.release()
        real_save = kit_update.save_state

        def fail_commit(path, state, **changes):
            if changes.get("version") == "1.10.0":
                raise OSError("simulated version record failure")
            real_save(path, state, **changes)

        with patch.object(kit_update, "save_state", side_effect=fail_commit):
            code, _, errors = self.run_cli("--apply", "--dsh-version", "0.4.0", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("simulated version record failure", errors)
        self.assertIsNone(self.state()["pending"])
        self.assert_original()

    def test_interrupted_switch_is_recovered_before_another_update(self):
        stage = self.root / ".kit-stage-interrupted"
        (stage / "candidate").mkdir(parents=True)
        (stage / "candidate" / "payload.txt").write_text("unfinished", encoding="utf-8")
        backup = self.root / ".kit-backup-interrupted"
        self.install.rename(backup)
        (stage / "candidate").rename(self.install)
        self.record.write_text(json.dumps({
            "version": "1.9.0",
            "pending": {"operation": "apply", "source": stage.name, "saved": backup.name, "hadCurrent": True},
        }), encoding="utf-8")
        self.release(version="1.9.0")

        code, output, errors = self.run_cli("--apply", "--yes")
        self.assertEqual(code, 0, errors)
        self.assertIn("No update", output)
        self.assertIsNone(self.state()["pending"])
        self.assert_original()

    def test_github_raw_manifest_resolves_the_release_asset(self):
        url = kit_update.asset_url(kit_update.DEFAULT_MANIFEST_URL, "0.2.0", "kit-0.2.0.zip", None)
        self.assertTrue(url.endswith("/releases/download/v0.2.0/kit-0.2.0.zip"))
        self.assertTrue(url.startswith("https://github.com/"))


if __name__ == "__main__":
    unittest.main()
