"""Offline reference scans must preserve the same privacy boundaries as online diff."""
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msshi_doctor_app import diff_engine as diff
from msshi_doctor_app.constants import EXIT_NEEDS_ATTENTION, UPSTREAM_FULL_NAME
from msshi_doctor_app.github_client import TreeSnapshot


class OfflineScopeTests(unittest.TestCase):
    def test_only_selected_files_are_opened_and_directories_entered(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in (
                "README.md", "docs/guide.md", "scripts/check.py", "scripts/auth.json",
                "scripts/.env.test", "scripts/private.key", "scripts/1. Private/note.md",
                "scripts/node_modules/lib/code.js", "scripts/.git/notes", "scripts/junction/notes",
                "scripts/alias.py", ".env", "user-space/data.json",
            ):
                path = root / rel
                path.parent.mkdir(exist_ok=True, parents=True)
                path.write_bytes(b"fixture")
            original_read = Path.read_bytes
            original_scandir = os.scandir
            for scope in ("core", "full"):
                read, scanned = set(), set()

                def track_read(path):
                    read.add(path.relative_to(root).as_posix())
                    return original_read(path)

                def track_scan(path):
                    scanned.add(Path(path).relative_to(root).as_posix())
                    return original_scandir(path)

                with (patch.object(Path, "read_bytes", track_read),
                      patch.object(os, "scandir", track_scan),
                      patch.object(diff, "is_reparse_or_symlink",
                                   side_effect=lambda p: p.name in {"junction", "alias.py"})):
                    snapshot = diff.build_local_tree_snapshot(root, scope=scope)
                expected = {"scripts/check.py", "README.md"}
                directories = {".", "scripts"}
                if scope == "full":
                    expected.add("docs/guide.md")
                    directories.add("docs")
                self.assertEqual(read, expected)
                self.assertEqual(set(snapshot.entries), expected)
                self.assertEqual(scanned, directories)

    def test_local_reference_never_claims_github_health_or_offers_repair(self):
        offline = Mock()
        offline.fetch_tree.return_value = TreeSnapshot(
            status="OFFLINE", repo=UPSTREAM_FULL_NAME, ref="main", commit_sha=None, entries={}
        )
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            ref, target = base / "reference", base / "target"
            for p in (ref, target):
                p.mkdir()
                (p / "README.md").write_text("matching content", encoding="utf-8")
            same_root = diff.run_online_diff(target, local_fallback_root=target, client=offline)
            self.assertFalse(same_root["used_local_fallback"])
            self.assertEqual(same_root["exit_code"], EXIT_NEEDS_ATTENTION)
            exact = diff.run_online_diff(target, local_fallback_root=ref, client=offline)
            self.assertEqual(exact["summary"]["healthy"], 1)
            self.assertEqual(exact["status"], "OFFLINE_WITH_LOCAL_FALLBACK")
            self.assertEqual(exact["exit_code"], EXIT_NEEDS_ATTENTION)
            (target / "README.md").unlink()
            drift = diff.run_online_diff(target, local_fallback_root=ref, client=offline)
            self.assertEqual(drift["summary"]["missing"], 1)
            self.assertEqual(drift["summary"]["repairable_count"], 0)
            self.assertFalse(drift["files"][0]["repairable"])
