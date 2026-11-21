"""The read-only troubleshooter must distinguish missing evidence from a healthy result."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msshi_doctor_app import safety
from msshi_doctor_app import cli
from msshi_doctor_app import troubleshooter as ts


class TroubleshooterEvidenceTests(unittest.TestCase):
    def test_incomplete_cli_result_requires_attention(self):
        report = {"status": "INCOMPLETE", "warnings": 0, "unknown_checks": 1, "checks": []}
        with patch.object(cli, "run_all_troubleshooter_checks", return_value=report), patch.object(cli, "_emit_json"):
            self.assertEqual(cli.run_cli(["--troubleshoot", "--json"]), 2)
            self.assertEqual(report["exit_code"], 2)

    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.root = Path(self.resources.enter_context(tempfile.TemporaryDirectory()))

    def test_lightcraft_policy_and_process_are_independent(self):
        policy = self.root / "msshi-runtime/policy.json"
        policy.parent.mkdir()
        for value, process, status in ((True, False, "WARN"), (False, False, "OK"), (False, True, "WARN"),
                                       ("false", False, "UNKNOWN"), (False, None, "UNKNOWN"), (None, None, "UNKNOWN")):
            with self.subTest(value=value, process=process):
                policy.write_text(json.dumps({"auto_open_on_boot": value}), encoding="utf-8")
                with patch.object(ts, "_running_lightcraft", return_value=process):
                    report = ts.check_lightcraft_lock(self.root)
                self.assertEqual(report["status"], status)
                self.assertIs(report["details"]["lightcraft_process_active"], process)
                self.assertIs(report["details"]["auto_open_on_boot"], value if isinstance(value, bool) else None)
                if value is not False:
                    self.assertNotIn("auto_open_on_boot=false", report["summary"])
        policy.unlink()
        with patch.object(ts, "_running_lightcraft", return_value=False):
            self.assertEqual(ts.check_lightcraft_lock(self.root)["status"], "UNKNOWN")

    def test_failed_or_unreadable_process_inventory_is_unknown(self):
        host = Mock(wraps=os)
        host.name = "nt"
        with patch.object(ts, "os", host), patch.object(ts.subprocess, "run") as run:
            for rc, output, expected in ((1, "", None), (0, "???", None),
                                         (0, '"lightcraft-helper.exe","11","Console","1","10 K"', False),
                                         (0, '"LightCraft.exe","11","Console","1","10 K"', True)):
                run.return_value = Mock(stdout=output, returncode=rc)
                self.assertIs(ts._running_lightcraft(), expected)
            run.side_effect = OSError("unavailable")
            self.assertIsNone(ts._running_lightcraft())

    def test_gateway_reads_bounded_tail_without_leaking_lines(self):
        self.assertEqual(ts.check_gateway_health(self.root)["status"], "UNKNOWN")
        log = self.root / "logs/gateway.log"
        log.parent.mkdir()
        log.touch()
        self.assertEqual(ts.check_gateway_health(self.root)["status"], "UNKNOWN")
        log.write_bytes(b"error old\n" + b"ok\n" * ts.LOG_TAIL_BYTES + b"connection timed out: secret-marker\n")
        real_open = Path.open
        reads = []

        class BoundedFile:
            def __enter__(self):
                self.f = real_open(log, "rb")
                return self

            def seek(self, *args):
                return self.f.seek(*args)

            def read(self, count):
                reads.append(count)
                return self.f.read(count)

            def __exit__(self, *args):
                self.f.close()

        with patch.object(Path, "open", autospec=True, return_value=BoundedFile()):
            report = ts.check_gateway_health(self.root)
        self.assertEqual(reads, [ts.LOG_TAIL_BYTES])
        self.assertNotIn("secret-marker", json.dumps(report))
        self.assertEqual(report["status"], "WARN")
        stats = report["details"]["logs"]["gateway.log"]
        self.assertEqual((stats["sampled_lines"], stats["recent_errors"], stats["recent_timeouts"]), (300, 0, 1))

    def test_protected_and_linked_roots_are_never_read_or_walked(self):
        paths = ("skills/1. Private", "profiles/junction/skills", "logs", "msshi-runtime")
        for rel in paths:
            (self.root / rel).mkdir(parents=True)
        for rel in ("logs/gateway.log", "msshi-runtime/policy.json", "skills/filelink.bin"):
            (self.root / rel).write_text("anything")
        linked = lambda p: p.name in {"junction", "gateway.log", "policy.json", "filelink.bin"}
        walk = os.walk
        visited = []
        def spy_walk(*args, **kwargs):
            for row in walk(*args, **kwargs):
                visited.append(Path(row[0]))
                yield row
        with (patch.object(safety, "is_reparse_or_symlink", side_effect=linked),
              patch.object(ts, "is_reparse_or_symlink", side_effect=linked),
              patch.object(ts, "_running_lightcraft", return_value=False),
              patch.object(ts.os, "walk", side_effect=spy_walk),
              patch.object(Path, "open", side_effect=AssertionError("opened forbidden path"))):
            self.assertEqual(ts.check_gateway_health(self.root)["status"], "UNKNOWN")
            self.assertEqual(ts.check_lightcraft_lock(self.root)["status"], "UNKNOWN")
            self.assertEqual(ts.check_skills_timeout(self.root)["details"]["roots_checked"], 1)
        self.assertEqual(visited, [self.root / "skills"])
