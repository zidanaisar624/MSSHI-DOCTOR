"""Exercise the public CLI with fixture installations and an in-memory release."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msshi_doctor_app import cli, local_probe, repair_engine
from msshi_doctor_app.constants import EXIT_CORE_ERROR, EXIT_NEEDS_ATTENTION, UPSTREAM_FULL_NAME
from msshi_doctor_app.github_client import TreeSnapshot
from msshi_doctor_app.safety import git_blob_sha1, sha256_bytes


class RuntimeRepairTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.base = Path(fixture.name)
        self.hermes = self.base / "hermes"
        self.launcher = self.base / "launcher"
        self.target = self.base / "release"
        for folder in (self.hermes, self.launcher, self.target):
            folder.mkdir()
        self.file = self.target / "README.md"
        self.release = b"official release\n"
        self.client = Mock()
        self.snapshot = TreeSnapshot(
            status="ONLINE_OK", repo=UPSTREAM_FULL_NAME, ref="v3.1.2", commit_sha="1" * 40,
            entries={"README.md": {"sha": git_blob_sha1(self.release), "size": len(self.release)}},
        )
        self.client.fetch_tree.return_value = self.snapshot
        self.client.download_pinned_file.return_value = self.release

    def call_cli(self, *args, as_json=True):
        options = ["--hermes-home", str(self.hermes), "--msshi-home", str(self.launcher),
                   "--target-root", str(self.target), "--no-cache", *args]
        if as_json:
            options.append("--json")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.run_cli(options, client=self.client)
        return code, json.loads(buf.getvalue()) if as_json else buf.getvalue()

    def test_native_scan_fix_and_recheck_use_selected_homes(self):
        (self.launcher / "launch.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps(dict(status='HEALTHY', exit_code=0, argv=sys.argv[1:], "
            "launcher=os.environ['MSSHI_HOME'], hermes=os.environ['HERMES_HOME'])))\n", encoding="utf-8",
        )
        with patch.dict(os.environ, {"MSSHI_HOME": str(self.base / "other-launcher"),
                                     "HERMES_HOME": str(self.base / "other-hermes")}):
            scan_code, scan = self.call_cli("--local")
            fix_code, fix = self.call_cli("--local-fix", "--apply")
            apply_code, applied = self.call_cli("--online-fix", "--apply")
        self.assertEqual((scan_code, fix_code, apply_code), (0, 0, 0))
        for result in (scan["doctor"], scan["runtime"], scan["interaction"], scan["trim_default"],
                       scan["connect_default"], fix["data"], applied["post_repair_doctor"]):
            self.assertEqual((result["launcher"], result["hermes"]), (str(self.launcher), str(self.hermes)))
        self.assertEqual(fix["data"]["argv"], ["doctor", "--fix", "--json"])
        self.assertEqual(applied["post_repair_doctor"]["argv"], ["doctor", "--json"])

    def test_successful_repair_reports_an_unavailable_post_check_separately(self):
        code, result = self.call_cli("--online-fix", "--apply")
        self.assertTrue(result["applied"])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(code, EXIT_NEEDS_ATTENTION)
        self.assertEqual(result["post_repair_exit_code"], EXIT_NEEDS_ATTENTION)
        self.assertEqual(self.file.read_bytes(), self.release)

    def test_local_fix_with_no_changes_is_explained_in_terminal(self):
        (self.launcher / "launch.py").write_text(
            "import json\nprint(json.dumps({'repair': {'applied': False, 'files_restored': 0}}))\n",
            encoding="utf-8",
        )
        code, message = self.call_cli("--local-fix", "--apply", as_json=False)
        self.assertEqual(code, 0)
        self.assertIn("tidak mengubah file", message)

    def test_passive_or_unavailable_install_cannot_report_healthy(self):
        with patch.object(local_probe.shutil, "which", side_effect=AssertionError("unrelated PATH")):
            code, scan = self.call_cli("--local")
            self.assertEqual(code, EXIT_NEEDS_ATTENTION)
            self.assertEqual(scan["status"], "NEEDS_ATTENTION")
            self.target.rmdir()
            with patch.object(local_probe, "_run_msshi_json", side_effect=AssertionError("native call")):
                code, scan = self.call_cli("--local")
                self.assertEqual(code, EXIT_CORE_ERROR)
                self.assertEqual(scan["status"], "CORE_ERROR")
            code, result = self.call_cli("--local-fix", "--apply")
            self.assertEqual(code, EXIT_CORE_ERROR)
            self.assertFalse(result["ok"])

    def test_empty_offline_tree_blocks_diff_preview_and_apply(self):
        self.snapshot.status = "OFFLINE"
        self.snapshot.entries = {}
        self.snapshot.commit_sha = None
        for args in (("--online",), ("--online-fix", "--dry-run"), ("--online-fix", "--apply")):
            code, result = self.call_cli(*args, "--no-fallback")
            self.assertEqual(code, EXIT_NEEDS_ATTENTION)
            if args[0] == "--online-fix":
                self.assertFalse(result["applied"])
                self.assertFalse(result["can_apply"])
        _, message = self.call_cli("--online", "--no-fallback", as_json=False)
        self.assertNotIn("identik dengan rilis GitHub", message)
        self.assertIn("belum terverifikasi", message)
        self.client.download_pinned_file.assert_not_called()
        self.assertFalse((self.hermes / "msshi-runtime").exists())

    def test_stale_preview_detects_new_or_edited_file(self):
        for previous in (None, b"previous content"):
            if previous is None:
                self.file.unlink(missing_ok=True)
            else:
                self.file.write_bytes(previous)
            plan = repair_engine.plan_online_repair(self.target, self.hermes, client=self.client)
            expected = {row["rel_path"]: row["local_sha256"] for row in plan["planned_files"]}
            self.assertEqual(expected, {"README.md": sha256_bytes(previous) if previous is not None else None})
            self.file.write_bytes(b"user edit")
            with self.assertRaisesRegex(ValueError, "pratinjau"):
                repair_engine.apply_online_repair(
                    self.target, self.hermes, client=self.client, expected_local_sha256=expected,
                )
            self.assertEqual(self.file.read_bytes(), b"user edit")
        self.client.download_pinned_file.assert_not_called()

    def test_change_during_download_is_preserved(self):
        def download(*args):
            self.file.write_bytes(b"new work")
            return self.release

        self.client.download_pinned_file.side_effect = download
        with self.assertRaisesRegex(ValueError, "unduhan"):
            repair_engine.apply_online_repair(self.target, self.hermes, client=self.client, run_recheck=False)
        self.assertEqual(self.file.read_bytes(), b"new work")
        self.assertFalse((self.hermes / "msshi-runtime").exists())

    def test_receipt_failure_restores_original_files(self):
        self.file.write_bytes(b"original")
        write_json = repair_engine.atomic_write_json

        def fail_to_finalize(path, payload):
            if payload["state"] == "applied":
                raise OSError("simulated full disk")
            write_json(path, payload)

        with patch.object(repair_engine, "atomic_write_json", side_effect=fail_to_finalize):
            with self.assertRaises(OSError):
                repair_engine.apply_online_repair(self.target, self.hermes, client=self.client, run_recheck=False)
        self.assertEqual(self.file.read_bytes(), b"original")
        receipts = list((self.hermes / "msshi-runtime/profile-receipts").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_bytes())["state"], "aborted_rolled_back")
