"""Comprehensive automated test suite for MSSHI Doctor App (offline + mock GitHub HTTP server)."""
from __future__ import annotations

from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from msshi_doctor_app import cli
from msshi_doctor_app.constants import (
    EXIT_CORE_ERROR,
    EXIT_HEALTHY,
    EXIT_NEEDS_ATTENTION,
    UPSTREAM_FULL_NAME,
)
from msshi_doctor_app.diff_engine import run_online_diff
from msshi_doctor_app.github_client import GitHubClient
from msshi_doctor_app.repair_engine import (
    apply_online_repair,
    plan_online_repair,
    rollback_online_repair,
)
from msshi_doctor_app.safety import (
    SecurityViolationError,
    git_blob_sha1,
    is_zone2_user_owned,
    is_zone3_protected,
    normalize_rel_path,
    reject_links,
    safe_join_within_root,
    sha256_bytes,
    validate_remote_url,
)

MOCK_COMMIT_SHA = "3c997aab915352f85d72eea9be8873ddc918c15e"
MOCK_REF = "v3.1.2"


class MockGitHubState:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.commit_sha = MOCK_COMMIT_SHA
        self.commit_etag = '"commit-etag-v312"'
        self.tree_etag = '"tree-etag-v312"'
        self.rate_limit_mode: int | None = None
        self.corrupt_raw_paths: set[str] = set()
        self.requested_raw_paths: list[str] = []
        self.commit_hits = 0
        self.tree_hits = 0


def start_mock_github_server(state: MockGitHubState) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def _send_json(self, code: int, payload: dict, extra_headers: dict[str, str] | None = None) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-RateLimit-Remaining", "0" if state.rate_limit_mode else "58")
            self.send_header("X-RateLimit-Reset", "1893456000")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if state.rate_limit_mode in (403, 429):
                self._send_json(state.rate_limit_mode, {"message": "API rate limit exceeded"})
                return

            commit_prefix = f"/repos/{UPSTREAM_FULL_NAME}/commits/"
            tree_prefix = f"/repos/{UPSTREAM_FULL_NAME}/git/trees/{state.commit_sha}"
            raw_prefix = f"/{UPSTREAM_FULL_NAME}/{state.commit_sha}/"

            if self.path.startswith(commit_prefix):
                state.commit_hits += 1
                if self.headers.get("If-None-Match") == state.commit_etag:
                    self.send_response(304)
                    self.send_header("ETag", state.commit_etag)
                    self.send_header("X-RateLimit-Remaining", "58")
                    self.end_headers()
                    return
                self._send_json(200, {"sha": state.commit_sha}, {"ETag": state.commit_etag})
                return

            if self.path.startswith(tree_prefix):
                state.tree_hits += 1
                if self.headers.get("If-None-Match") == state.tree_etag:
                    self.send_response(304)
                    self.send_header("ETag", state.tree_etag)
                    self.send_header("X-RateLimit-Remaining", "57")
                    self.end_headers()
                    return
                tree_entries = [
                    {
                        "path": rel,
                        "type": "blob",
                        "sha": git_blob_sha1(data),
                        "size": len(data),
                    }
                    for rel, data in sorted(state.files.items())
                ]
                self._send_json(
                    200,
                    {"sha": state.commit_sha, "truncated": False, "tree": tree_entries},
                    {"ETag": state.tree_etag},
                )
                return

            if self.path.startswith(raw_prefix):
                rel = self.path[len(raw_prefix) :]
                state.requested_raw_paths.append(rel)
                if rel not in state.files:
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = b"TAMPERED_PAYLOAD_BAD_HASH" if rel in state.corrupt_raw_paths else state.files[rel]
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


class TestOnlineDiffAndSafeRepair(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.target_root = self.base / "msshi-release"
        self.hermes_home = self.base / "HermesData"
        self.cache_dir = self.base / "etag-cache"
        self.target_root.mkdir(parents=True)
        self.hermes_home.mkdir(parents=True)

        self.upstream_files: dict[str, bytes] = {
            "VERSION.json": b'{\n  "version": "3.1.2",\n  "channel": "stable"\n}\n',
            "scripts/msshi_cli.py": b'"""MSSHI CLI core."""\ndef main():\n    return 0\n',
            "scripts/helper.py": b"def helper():\n    return 'ok'\n",
            "configs/presets.json": b'{"presets": ["balanced", "lite"]}\n',
            "hermes_plugins/msshi-auto-trim/plugin.yaml": b"name: msshi-auto-trim\nversion: 3.1.2\n",
            "skills/demo-skill/SKILL.md": b"# Demo Skill\nClean UTF-8 skill instruction.\n",
            # Upstream non-core file (excluded in scope=core)
            ".github/workflows/ci.yml": b"name: ci\n",
        }
        self.state = MockGitHubState(self.upstream_files)
        self.server, self.base_url = start_mock_github_server(self.state)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _make_client(self, *, use_cache: bool = True) -> GitHubClient:
        return GitHubClient(
            api_base=self.base_url,
            raw_base=self.base_url,
            cache_dir=self.cache_dir if use_cache else None,
            allow_loopback_hosts=frozenset({"127.0.0.1"}),
            timeout=5.0,
        )

    def _write_local(self, rel: str, data: bytes) -> Path:
        dest = self.target_root.joinpath(*rel.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def _populate_healthy_local(self) -> None:
        for rel, data in self.upstream_files.items():
            if rel.startswith(".github/"):
                continue
            self._write_local(rel, data)

    def test_diff_detects_healthy_crlf_missing_corrupted_and_drifted(self) -> None:
        # Healthy (with Windows CRLF line endings!)
        self._write_local("VERSION.json", self.upstream_files["VERSION.json"].replace(b"\n", b"\r\n"))
        # Healthy exact
        self._write_local("skills/demo-skill/SKILL.md", self.upstream_files["skills/demo-skill/SKILL.md"])
        # MISSING: scripts/helper.py (intentionally omitted)
        # CORRUPTED (NUL byte): hermes_plugins/msshi-auto-trim/plugin.yaml
        self._write_local("hermes_plugins/msshi-auto-trim/plugin.yaml", b"name: bad\x00null")
        # CORRUPTED (invalid JSON): configs/presets.json
        self._write_local("configs/presets.json", b"{broken json")
        # DRIFTED (valid Python, different content): scripts/msshi_cli.py
        self._write_local("scripts/msshi_cli.py", b'def main():\n    return 42\n')

        client = self._make_client()
        report = run_online_diff(self.target_root, ref=MOCK_REF, hermes_home=self.hermes_home, client=client)

        by_path = {row["rel_path"]: row for row in report["files"]}
        self.assertEqual(report["connection_status"], "ONLINE_OK")
        self.assertEqual(report["commit_sha"], MOCK_COMMIT_SHA)
        self.assertEqual(report["exit_code"], EXIT_CORE_ERROR)
        self.assertEqual(by_path["VERSION.json"]["status"], "HEALTHY")
        self.assertEqual(by_path["skills/demo-skill/SKILL.md"]["status"], "HEALTHY")
        self.assertEqual(by_path["scripts/helper.py"]["status"], "MISSING")
        self.assertEqual(by_path["hermes_plugins/msshi-auto-trim/plugin.yaml"]["status"], "CORRUPTED")
        self.assertEqual(by_path["hermes_plugins/msshi-auto-trim/plugin.yaml"]["reason"], "nul_byte")
        self.assertEqual(by_path["configs/presets.json"]["status"], "CORRUPTED")
        self.assertEqual(by_path["configs/presets.json"]["reason"], "invalid_json")
        self.assertEqual(by_path["scripts/msshi_cli.py"]["status"], "DRIFTED")
        # .github workflow excluded in core scope
        self.assertNotIn(".github/workflows/ci.yml", by_path)

        # Second call hits ETag 304 cache without re-fetching tree!
        prev_tree_hits = self.state.tree_hits
        report2 = run_online_diff(self.target_root, ref=MOCK_REF, hermes_home=self.hermes_home, client=client)
        self.assertEqual(report2["connection_status"], "ONLINE_ETAG_CACHED")
        self.assertTrue(report2["from_cache"])
        self.assertEqual(self.state.tree_hits, prev_tree_hits)

    def test_corruption_variants_utf8_empty_and_python_syntax(self) -> None:
        self._populate_healthy_local()
        self._write_local("skills/demo-skill/SKILL.md", b"\xff\xfeinvalid-utf8")
        self._write_local("configs/presets.json", b"")
        self._write_local("scripts/helper.py", b"def broken(:\n    pass\n")

        report = run_online_diff(self.target_root, ref=MOCK_REF, client=self._make_client())
        by_path = {r["rel_path"]: r for r in report["files"]}
        self.assertEqual(by_path["skills/demo-skill/SKILL.md"]["reason"], "invalid_utf8")
        self.assertEqual(by_path["configs/presets.json"]["reason"], "empty_file")
        self.assertEqual(by_path["scripts/helper.py"]["reason"], "invalid_python_syntax")

    def test_six_step_repair_backup_receipt_and_one_click_rollback(self) -> None:
        self._populate_healthy_local()
        # Introduce 1 MISSING, 1 DRIFTED, 1 CORRUPTED
        (self.target_root / "scripts/helper.py").unlink()
        drifted_bytes = b'def main():\n    return 999\n'
        corrupted_bytes = b"{invalid json!"
        self._write_local("scripts/msshi_cli.py", drifted_bytes)
        self._write_local("configs/presets.json", corrupted_bytes)

        client = self._make_client()

        # Step 1: Dry-run preview must NOT write anything
        plan = plan_online_repair(self.target_root, self.hermes_home, ref=MOCK_REF, client=client)
        self.assertTrue(plan["dry_run"])
        self.assertTrue(plan["can_apply"])
        self.assertEqual(plan["planned_count"], 3)
        self.assertFalse((self.hermes_home / "msshi-runtime").exists())
        self.assertFalse((self.target_root / "scripts/helper.py").exists())

        # Step 2-5: Apply selective 6-step repair
        result = apply_online_repair(
            self.target_root,
            self.hermes_home,
            ref=MOCK_REF,
            client=client,
            run_recheck=False,
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(result["files_repaired"], 3)
        self.assertEqual(result["exit_code"], EXIT_HEALTHY)
        # Verify selective download: ONLY the 3 broken files were downloaded!
        self.assertEqual(
            sorted(self.state.requested_raw_paths),
            ["configs/presets.json", "scripts/helper.py", "scripts/msshi_cli.py"],
        )
        # Receipt & backups exist
        receipt_path = Path(result["receipt_path"])
        backup_dir = Path(result["backup_dir"])
        self.assertTrue(receipt_path.is_file())
        self.assertTrue(backup_dir.is_dir())

        # Post-repair diff must be 100% HEALTHY
        after_diff = run_online_diff(self.target_root, ref=MOCK_REF, hermes_home=self.hermes_home, client=client)
        self.assertEqual(after_diff["status"], "HEALTHY")
        self.assertEqual(after_diff["exit_code"], EXIT_HEALTHY)

        # Rollback Preview (dry-run) must NOT mutate disk
        rb_preview = rollback_online_repair(self.hermes_home, result["receipt_id"], apply=False)
        self.assertTrue(rb_preview["dry_run"])
        self.assertEqual(len(rb_preview["restored_files"]), 2)
        self.assertEqual(rb_preview["removed_created_files"], ["scripts/helper.py"])
        self.assertTrue((self.target_root / "scripts/helper.py").is_file())

        # 1-Click Rollback Apply restores byte-for-byte & deletes newly created file
        rb_apply = rollback_online_repair(self.hermes_home, result["receipt_id"], apply=True)
        self.assertEqual(rb_apply["state"], "rolled_back")
        self.assertEqual(rb_apply["conflicts"], [])
        self.assertFalse((self.target_root / "scripts/helper.py").exists())
        self.assertEqual((self.target_root / "scripts/msshi_cli.py").read_bytes(), drifted_bytes)
        self.assertEqual((self.target_root / "configs/presets.json").read_bytes(), corrupted_bytes)

    def test_rollback_conflict_guard_preserves_post_repair_user_edit(self) -> None:
        self._populate_healthy_local()
        self._write_local("scripts/msshi_cli.py", b"old_drifted = 1\n")
        client = self._make_client()
        res = apply_online_repair(self.target_root, self.hermes_home, ref=MOCK_REF, client=client, run_recheck=False)

        # User manually edits file after repair
        manual_edit = b"# user edit after repair\n"
        self._write_local("scripts/msshi_cli.py", manual_edit)

        rb = rollback_online_repair(self.hermes_home, res["receipt_id"], apply=True)
        self.assertEqual(rb["state"], "rollback_conflicts")
        self.assertEqual(rb["exit_code"], EXIT_NEEDS_ATTENTION)
        self.assertEqual(len(rb["conflicts"]), 1)
        self.assertEqual((self.target_root / "scripts/msshi_cli.py").read_bytes(), manual_edit)

    def test_checksum_mismatch_aborts_before_touching_disk(self) -> None:
        self._populate_healthy_local()
        original = b"local_before_abort = True\n"
        self._write_local("scripts/msshi_cli.py", original)
        self.state.corrupt_raw_paths.add("scripts/msshi_cli.py")

        client = self._make_client()
        with self.assertRaises(ValueError):
            apply_online_repair(self.target_root, self.hermes_home, ref=MOCK_REF, client=client, run_recheck=False)
        # Original file untouched & no corrupted half-written state
        self.assertEqual((self.target_root / "scripts/msshi_cli.py").read_bytes(), original)

    def test_mid_flight_write_failure_triggers_automatic_rollback(self) -> None:
        self._populate_healthy_local()
        orig_a = b"a_drifted = 1\n"
        orig_b = b"b_drifted = 2\n"
        self._write_local("scripts/helper.py", orig_a)
        self._write_local("scripts/msshi_cli.py", orig_b)

        from msshi_doctor_app import repair_engine

        real_atomic = repair_engine.atomic_write_bytes
        target_writes = {"count": 0}

        def flaky_write(path: Path, data: bytes, *, mode=None):
            if str(path).startswith(str(self.target_root)):
                target_writes["count"] += 1
                if target_writes["count"] == 2:
                    raise OSError("Simulated mid-flight disk I/O error")
            return real_atomic(path, data, mode=mode)

        client = self._make_client()
        with mock.patch.object(repair_engine, "atomic_write_bytes", side_effect=flaky_write):
            with self.assertRaises(OSError):
                apply_online_repair(self.target_root, self.hermes_home, ref=MOCK_REF, client=client, run_recheck=False)

        # First file must be automatically rolled back to orig_a, second remains orig_b!
        self.assertEqual((self.target_root / "scripts/helper.py").read_bytes(), orig_a)
        self.assertEqual((self.target_root / "scripts/msshi_cli.py").read_bytes(), orig_b)
        receipts = list((self.hermes_home / "msshi-runtime/profile-receipts").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        rec = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(rec["state"], "aborted_rolled_back")

    def test_local_snapshot_five_honest_statuses_zero_credits(self) -> None:
        from msshi_doctor_app.local_probe import collect_local_snapshot

        snap = collect_local_snapshot(
            hermes_home=self.hermes_home,
            msshi_home=self.base / "msshi-home",
            target_root=self.target_root,
            run_cli=False,
        )
        self.assertTrue(snap["read_only"])
        self.assertEqual(snap["model_calls"], 0)
        self.assertFalse(snap["secrets_exposed"])
        self.assertEqual(len(snap["five_statuses"]), 5)
        self.assertEqual(snap["five_statuses"][4]["badge"], "NOT_TESTED_0_CREDITS")

    def test_zone2_and_zone3_absolute_protection_and_zero_secret_leak(self) -> None:
        self._populate_healthy_local()
        self._write_local("scripts/msshi_cli.py", b"drifted = 1\n")

        # Create realistic Zone 2 user-owned files with sentinel secrets
        sentinel = "SENTINEL_FAKE_API_KEY_987654321"
        zone2_files = {
            self.hermes_home / "SOUL.md": b"# Custom User SOUL\nNever overwrite me.\n",
            self.hermes_home / "profiles/yukino/SOUL.md": b"broken\x00soul",  # Corrupted SOUL -> warn only!
            self.hermes_home / "config.yaml": f"model:\n  api_key: {sentinel}\n".encode(),
            self.hermes_home / ".env": f"OPENAI_API_KEY={sentinel}\n".encode(),
            self.hermes_home / "auth.json": json.dumps({"token": sentinel}).encode(),
        }
        # Create Zone 3 strict protected paths
        zone3_files = {
            self.base / "1. Private/personal.txt": f"private {sentinel}".encode(),
            self.base / "2. Data Diri atau Perkembangan Diri (NP)/journal.md": b"human only",
            self.hermes_home / "data/bin/nlm.exe": b"MZ_FAKE_NLM",
            self.hermes_home / "data/bin/notebooklm-mcp.exe": b"MZ_FAKE_MCP",
            self.base / ".config/rclone/rclone.conf": f"[gdrive]\ntoken={sentinel}\n".encode(),
            self.base / "MSSHI_Recovery/backup.zip": b"PK_FAKE",
        }
        before_hashes = {}
        for path, content in {**zone2_files, **zone3_files}.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            before_hashes[path] = sha256_bytes(content)

        client = self._make_client()
        diff = run_online_diff(self.target_root, ref=MOCK_REF, hermes_home=self.hermes_home, client=client)
        apply_res = apply_online_repair(
            self.target_root, self.hermes_home, ref=MOCK_REF, client=client, run_recheck=False
        )
        receipt_text = Path(apply_res["receipt_path"]).read_text(encoding="utf-8")

        # Assert zero secrets leaked in any JSON output or receipt
        combined_output = json.dumps(diff) + json.dumps(apply_res) + receipt_text
        self.assertNotIn(sentinel, combined_output)

        # Assert EVERY Zone 2 & Zone 3 file is byte-for-byte untouched (even corrupted yukino SOUL.md!)
        for path, expected_hash in before_hashes.items():
            self.assertEqual(sha256_bytes(path.read_bytes()), expected_hash, f"Protected file mutated: {path}")

        # Passive SOUL check warned on yukino without auto-repairing
        self.assertEqual(diff["summary"]["zone2_soul_warnings"], 1)

        # Explicit attempts to target Zone 2 or Zone 3 must raise SecurityViolationError
        for forbidden in ("SOUL.md", ".env", "config.yaml", "auth.json", "data/bin/nlm.exe", "1. Private/x.txt"):
            with self.assertRaises(SecurityViolationError):
                plan_online_repair(
                    self.target_root,
                    self.hermes_home,
                    ref=MOCK_REF,
                    selected_files=[forbidden],
                    client=client,
                )

    def test_security_guards_traversal_hosts_and_reparse_links(self) -> None:
        # Path traversal & drive escape
        for bad in ("../escape.py", "scripts/../../outside.py", "/etc/passwd", "C:/Windows/win.ini", "a\x00b", ""):
            with self.assertRaises(SecurityViolationError):
                normalize_rel_path(bad)
            with self.assertRaises(SecurityViolationError):
                safe_join_within_root(self.target_root, bad)

        # Untrusted hosts, non-HTTPS, wrong GitHub repo, embedded credentials
        bad_urls = [
            f"http://api.github.com/repos/{UPSTREAM_FULL_NAME}/commits/v3.1.2",
            "https://evil.example.com/payload",
            "https://api.github.com/repos/otheruser/otherrepo/commits/main",
            f"https://user:pass@api.github.com/repos/{UPSTREAM_FULL_NAME}/commits/v3.1.2",
        ]
        for url in bad_urls:
            with self.assertRaises(SecurityViolationError):
                validate_remote_url(url)

        # Official URLs accepted
        validate_remote_url(f"https://api.github.com/repos/{UPSTREAM_FULL_NAME}/commits/v3.1.2")
        validate_remote_url(f"https://raw.githubusercontent.com/{UPSTREAM_FULL_NAME}/{MOCK_COMMIT_SHA}/VERSION.json")

        # Zone classifiers
        self.assertTrue(is_zone2_user_owned("D:/HermesData/SOUL.md"))
        self.assertTrue(is_zone2_user_owned("D:/HermesData/.env"))
        self.assertTrue(is_zone3_protected("D:/HermesProject/1. Private/secret.docx"))
        self.assertTrue(is_zone3_protected("D:/HermesProject/2. Data Diri atau Perkembangan Diri (NP)/a.md"))
        self.assertTrue(is_zone3_protected("D:/HermesData/data/bin/nlm.exe"))
        self.assertTrue(is_zone3_protected("D:/MSSHI_Recovery/file.txt"))

        # Windows junction / reparse point rejection (0x400 FILE_ATTRIBUTE_REPARSE_POINT)
        fake_stat = mock.MagicMock(st_mode=0o040777, st_file_attributes=0x400)
        with mock.patch("pathlib.Path.lstat", return_value=fake_stat), mock.patch("os.path.lexists", return_value=True):
            with self.assertRaises(SecurityViolationError):
                reject_links(self.target_root / "scripts/msshi_cli.py")

    def test_offline_and_rate_limit_graceful_fallback(self) -> None:
        self._populate_healthy_local()
        self._write_local("scripts/msshi_cli.py", b"drifted_offline = 1\n")

        # 1. Rate-limit HTTP 403 -> graceful RATE_LIMITED without crash
        self.state.rate_limit_mode = 403
        rl_report = run_online_diff(self.target_root, ref=MOCK_REF, client=self._make_client(use_cache=False))
        self.assertEqual(rl_report["connection_status"], "RATE_LIMITED")
        self.assertEqual(rl_report["rate_limit_remaining"], 0)
        self.assertEqual(rl_report["exit_code"], EXIT_NEEDS_ATTENTION)

        # 2. Offline unreachable port + local fallback root (msshi-upstream)
        fallback_dir = self.base / "msshi-upstream"
        for rel, data in self.upstream_files.items():
            p = fallback_dir.joinpath(*rel.split("/"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

        offline_client = GitHubClient(
            api_base="http://127.0.0.1:1",
            raw_base="http://127.0.0.1:1",
            allow_loopback_hosts=frozenset({"127.0.0.1"}),
            timeout=0.5,
        )
        fb_report = run_online_diff(
            self.target_root,
            ref=MOCK_REF,
            client=offline_client,
            local_fallback_root=fallback_dir,
        )
        self.assertEqual(fb_report["connection_status"], "OFFLINE_WITH_LOCAL_FALLBACK")
        self.assertTrue(fb_report["used_local_fallback"])
        self.assertEqual(fb_report["summary"]["drifted"], 1)

    def test_cli_end_to_end_json_and_exit_codes(self) -> None:
        self._populate_healthy_local()
        (self.target_root / "scripts/helper.py").unlink()
        client = self._make_client()

        def run_capture(argv: list[str]) -> tuple[int, dict]:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.run_cli(argv, client=client)
            return rc, json.loads(buf.getvalue())

        rc, ver = run_capture(["--version", "--json"])
        self.assertEqual(rc, EXIT_HEALTHY)
        self.assertEqual(ver["app"], "MSSHI Doctor")

        rc, diff = run_capture([
            "--online", "--json", "--ref", MOCK_REF,
            "--target-root", str(self.target_root),
            "--hermes-home", str(self.hermes_home),
            "--no-cache",
        ])
        self.assertEqual(rc, EXIT_NEEDS_ATTENTION)
        self.assertEqual(diff["summary"]["missing"], 1)

        rc, preview = run_capture([
            "--online-fix", "--dry-run", "--json", "--ref", MOCK_REF,
            "--target-root", str(self.target_root),
            "--hermes-home", str(self.hermes_home),
            "--no-cache",
        ])
        self.assertEqual(rc, EXIT_NEEDS_ATTENTION)
        self.assertTrue(preview["dry_run"])

        rc, applied = run_capture([
            "--online-fix", "--apply", "--no-recheck", "--json", "--ref", MOCK_REF,
            "--target-root", str(self.target_root),
            "--hermes-home", str(self.hermes_home),
            "--no-cache",
        ])
        self.assertEqual(rc, EXIT_HEALTHY)
        self.assertTrue(applied["applied"])
        rid = applied["receipt_id"]

        rc, receipts = run_capture(["--receipts", "--json", "--hermes-home", str(self.hermes_home)])
        self.assertEqual(rc, EXIT_HEALTHY)
        self.assertEqual(receipts["count"], 1)

        rc, rb = run_capture([
            "--rollback", rid, "--apply", "--json", "--hermes-home", str(self.hermes_home)
        ])
        self.assertEqual(rc, EXIT_HEALTHY)
        self.assertEqual(rb["state"], "rolled_back")
        self.assertFalse((self.target_root / "scripts/helper.py").exists())

        rc, ts = run_capture(["--troubleshoot", "--json", "--hermes-home", str(self.hermes_home)])
        self.assertIn(rc, (EXIT_HEALTHY, EXIT_NEEDS_ATTENTION))
        self.assertEqual(len(ts["checks"]), 5)


class TestBrandingAssetsAndPrivacyHygiene(unittest.TestCase):
    def test_branding_assets_and_multires_ico(self) -> None:
        assets = REPO_ROOT / "assets"
        required = [
            "msshi-doctor-banner.jpg",
            "msshi-doctor-banner-ui.png",
            "msshi-doctor-icon.png",
            "msshi-doctor-icon-256.png",
            "msshi-doctor-icon-64.png",
            "msshi-doctor.ico",
        ]
        for fname in required:
            p = assets / fname
            self.assertTrue(p.is_file(), f"Missing branding asset: {fname}")
            self.assertGreater(p.stat().st_size, 500)

        # Parse ICO directory header (pure stdlib) to verify multi-resolution sizes
        ico_bytes = (assets / "msshi-doctor.ico").read_bytes()
        _reserved, img_type, count = struct.unpack_from("<HHH", ico_bytes, 0)
        self.assertEqual(img_type, 1)
        self.assertGreaterEqual(count, 6)
        sizes = []
        for idx in range(count):
            w, h = struct.unpack_from("<BB", ico_bytes, 6 + idx * 16)
            sizes.append(256 if w == 0 else w)
            self.assertEqual(w, h)
        for required_size in (16, 24, 32, 48, 64, 128, 256):
            self.assertIn(required_size, sizes)

    def test_readme_uses_relative_branding_paths_and_zero_personal_paths(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("assets/msshi-doctor-banner.jpg", readme)
        self.assertIn("assets/msshi-doctor-icon-64.png", readme)

        forbidden_markers = [
            "C:\\Users\\",
            "C:/Users/",
            "D:\\Zidan",
            "D:/Zidan",
            "ghp_",
            "github_pat_",
            "sk-proj-",
            "AIzaSy",
        ]
        scan_files = [
            REPO_ROOT / "README.md",
            REPO_ROOT / "pyproject.toml",
            REPO_ROOT / "MSSHI_Doctor.bat",
            REPO_ROOT / "msshi-doctor.cmd",
            REPO_ROOT / "build/msshi-doctor.spec",
            *sorted((REPO_ROOT / "src/msshi_doctor_app").glob("*.py")),
        ]
        for fpath in scan_files:
            content = fpath.read_text(encoding="utf-8")
            for marker in forbidden_markers:
                self.assertNotIn(marker, content, f"Forbidden personal/secret marker {marker!r} in {fpath.name}")


if __name__ == "__main__":
    unittest.main()
