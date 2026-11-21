"""Exercise real Tk button callbacks with isolated homes and an in-memory GitHub fixture."""
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    from msshi_doctor_app import gui
except ModuleNotFoundError as exc:
    if exc.name not in {"tkinter", "_tkinter"}:
        raise
    gui = None


@unittest.skipIf(gui is None, "Tk unavailable")
class GuidedWindowTests(unittest.TestCase):
    pin = "1" * 40

    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        root = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))
        self.hermes = root / "hermes"
        self.hermes.mkdir()
        self.launcher = root / "launcher"
        self.target = root / "msshi"
        self.target.mkdir()
        self.target.joinpath("VERSION.json").write_text('{"version":"3.1.2"}', encoding="utf-8")
        self.target.joinpath("INSTALL_FOR_AGENTS.md").touch()
        self.snap = self.replace("collect_local_snapshot", return_value={"status": "HEALTHY"})
        self.diff = self.replace("run_online_diff")
        self.plan = self.replace("plan_online_repair")
        self.apply = self.replace("apply_online_repair")
        self.rollback = self.replace("rollback_online_repair")
        self.receipts = self.replace("discover_receipts", return_value=[])
        self.troubleshoot = self.replace("run_all_troubleshooter_checks", return_value={"checks": []})
        self.messages = self.replace("messagebox")
        self.replace("resolve_cache_dir", return_value=root / "cache")
        self.callback_errors = []

    def replace(self, name, **kwargs):
        return self.resources.enter_context(patch.object(gui, name, **kwargs))

    def start(self, target=None):
        try:
            self.app = gui.MSSSIDoctorGUI(hermes_home=self.hermes, msshi_home=self.launcher,
                                          target_root=target or self.target)
        except gui.tk.TclError as exc:
            if "display" in str(exc).lower():
                self.skipTest(str(exc))
            raise
        self.app.withdraw()
        self.addCleanup(self.app.destroy)
        self.app.report_callback_exception = lambda *args: self.callback_errors.append(args[1])
        self.wait_for(lambda: self.app._local_snapshot is not None)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.app.update()
            if self.callback_errors:
                raise self.callback_errors[0]
            if predicate():
                return
            time.sleep(0.01)
        self.fail("No matching GUI state before test timeout")

    def compare(self, *, connection="ONLINE_OK"):
        self.diff.return_value = {
            "connection_status": connection, "scope": "core", "commit_sha": self.pin, "github_ref": "v3.1.2",
            "summary": {"missing": 1, "drifted": 1},
            "files": [{"rel_path": "scripts/check.py", "status": "MISSING", "repairable": True},
                      {"rel_path": "README.md", "status": "DRIFTED", "repairable": True}],
        }
        self.app.guide_next.invoke()
        self.wait_for(lambda: self.app._diff_report is not None)

    def test_first_run_is_local_then_choose_an_install_with_one_missing_marker(self):
        self.start(self.target / "missing")
        self.assertEqual(self.app._active_page, "guide")
        self.assertEqual(self.app.guide_next["state"], "disabled")
        self.diff.assert_not_called()
        self.troubleshoot.assert_not_called()
        self.receipts.assert_not_called()
        with patch.object(gui.filedialog, "askdirectory", return_value=str(self.target)):
            self.app._choose_target()
        self.wait_for(lambda: self.app.guide_next["state"] == "normal")
        self.assertEqual(self.app.target_root, self.target)
        self.snap.assert_called_with(hermes_home=self.hermes, msshi_home=self.launcher,
                                     target_root=self.target, run_cli=True)
        self.app._show_page("troubleshooter")
        self.wait_for(lambda: "troubleshooter" not in self.app._working)
        self.troubleshoot.assert_called_once()

    def test_preview_cancel_and_confirm_are_distinct_and_apply_reviewed_bytes(self):
        self.start()
        self.compare()
        self.assertEqual(self.app._checked_paths, {"scripts/check.py"})
        self.app._toggle_diff_row("README.md")
        self.app._ref_var.set("main")
        self.assertEqual(self.app.repair_selected_btn["state"], "disabled")
        self.app._ref_var.set("v3.1.2")
        self.assertEqual(self.app.repair_selected_btn["state"], "normal")
        self.plan.return_value = {"commit_sha": self.pin, "can_apply": True, "planned_files": [
            {"status": "MISSING", "rel_path": "scripts/check.py", "local_sha256": None},
            {"status": "DRIFTED", "rel_path": "README.md", "local_sha256": "2" * 64}]}
        self.apply.return_value = {"state": "applied", "files_repaired": 2, "receipt_id": "demo"}

        def open_preview():
            self.app.repair_selected_btn.invoke()
            self.wait_for(lambda: any(isinstance(w, gui.SafeRepairConfirmModal) for w in self.app.winfo_children()))
            return next(w for w in self.app.winfo_children() if isinstance(w, gui.SafeRepairConfirmModal))

        modal = open_preview()
        self.apply.assert_not_called()
        self.assertEqual(self.plan.call_args.kwargs["ref"], self.pin)
        modal.cancel_btn.invoke()
        self.apply.assert_not_called()
        modal = open_preview()
        modal.apply_btn.invoke()
        self.wait_for(lambda: self.app._active_page == "receipts" and not self.app._working)
        self.assertEqual(self.apply.call_args.args, (self.target, self.hermes))
        kwargs = self.apply.call_args.kwargs
        self.assertEqual(kwargs["ref"], self.pin)
        self.assertEqual(kwargs["msshi_home"], self.launcher)
        self.assertCountEqual(kwargs["selected_files"], ["scripts/check.py", "README.md"])
        self.assertEqual(kwargs["expected_local_sha256"], {"scripts/check.py": None, "README.md": "2" * 64})
        self.receipts.assert_called_once_with(self.hermes)

    def test_offline_and_network_errors_allow_retry_without_repair(self):
        self.start()
        self.compare(connection="OFFLINE")
        self.assertEqual(self.app.repair_selected_btn["state"], "disabled")
        self.diff.side_effect = OSError("koneksi terputus")
        self.app.check_online_btn.invoke()
        self.wait_for(lambda: "diff" not in self.app._working)
        self.assertEqual(self.app.check_online_btn["state"], "normal")
        self.assertIsNone(self.app._diff_report)
        self.apply.assert_not_called()

    def test_rollback_cancel_then_confirm(self):
        self.start()
        self.app._show_page("receipts")
        self.wait_for(lambda: not self.app._working)
        self.app._selected_receipt = {"path": "example", "can_one_click_rollback": True}
        self.rollback.return_value = {"receipt_id": "example", "restored_files": ["README.md"]}
        self.messages.askyesno.return_value = False
        self.app._trigger_rollback()
        self.wait_for(lambda: not self.app._working)
        self.messages.askyesno.assert_called_once()
        self.rollback.assert_called_once_with(self.hermes, "example", apply=False)
        self.messages.askyesno.return_value = True
        self.app._trigger_rollback()
        self.wait_for(lambda: not self.app._working)
        self.assertEqual([c.kwargs["apply"] for c in self.rollback.call_args_list], [False, False, True])

    def test_native_fix_respects_both_selected_homes(self):
        self.start()
        fix = self.replace("run_local_doctor_fix", return_value={"ok": True})
        self.messages.askyesno.return_value = True
        self.app._prompt_local_doctor_fix()
        self.wait_for(lambda: not self.app._working)
        fix.assert_called_once_with(hermes_home=self.hermes, msshi_home=self.launcher)
