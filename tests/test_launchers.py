"""Smoke tests for the public entry points; no live installation is required."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_pyinstaller_script_can_run_as_main(self):
        # PyInstaller runs the entry script outside its package context.
        executable_script = ROOT / "src" / "msshi_doctor_app" / "__main__.py"
        proc = subprocess.run(
            [sys.executable, "-B", str(executable_script), "--version", "--json"],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True, text=True, check=True,
        )
        self.assertIn("version", json.loads(proc.stdout))

    @unittest.skipUnless(os.name == "nt", "Windows batch entry point")
    def test_windows_cli_preserves_diagnostic_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for mode, expected in [
                (["--version"], 0),
                (["--online"], 1),  # Explicit target is absent; no request is made.
                (["--local-fix", "--dry-run"], 2),
            ]:
                with self.subTest(mode=mode):
                    proc = subprocess.run(
                        [os.environ["COMSPEC"], "/d", "/c", "msshi-doctor.cmd",
                         *mode, "--json", "--hermes-home", str(root / "hermes"),
                         "--msshi-home", str(root / "msshi"), "--target-root", str(root / "missing")],
                        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                    )
                    self.assertEqual(proc.returncode, expected, proc.stderr)
                    self.assertIsInstance(json.loads(proc.stdout), dict)
