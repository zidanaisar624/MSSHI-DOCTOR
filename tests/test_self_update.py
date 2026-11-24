"""Offline unit tests for MSSHI Doctor App Self-Update engine (v1.2.0)."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.error import URLError
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from msshi_doctor_app.cli import run_cli
from msshi_doctor_app.constants import APP_REPO_FULL_NAME, APP_VERSION, SAWERIA_URL
from msshi_doctor_app.self_update import (
    check_app_update,
    is_newer_version,
    parse_semver,
    parse_sha256sums,
    run_self_update,
    stage_windows_self_update,
    validate_app_update_url,
)


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class SelfUpdateTests(unittest.TestCase):
    def test_semver_comparison(self) -> None:
        self.assertEqual(parse_semver("v1.2.0"), (1, 2, 0))
        self.assertTrue(is_newer_version("v1.3.0", "1.2.0"))
        self.assertFalse(is_newer_version("v1.2.0", "1.2.0"))
        self.assertFalse(is_newer_version("v1.1.9", "1.2.0"))
        self.assertFalse(is_newer_version("invalid", "1.2.0"))

    def test_strict_url_allowlist_blocks_foreign_hosts_and_repos(self) -> None:
        valid = f"https://api.github.com/repos/{APP_REPO_FULL_NAME}/releases/latest"
        self.assertEqual(validate_app_update_url(valid), valid)
        with self.assertRaises(ValueError):
            validate_app_update_url("http://api.github.com/repos/zidanaisar624/MSSHI-DOCTOR/releases/latest")
        with self.assertRaises(ValueError):
            validate_app_update_url("https://evil.example.com/doctor.zip")
        with self.assertRaises(ValueError):
            validate_app_update_url("https://api.github.com/repos/other/repo/releases/latest")

    def test_check_update_with_etag_304_and_sha256sums(self) -> None:
        with TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            zip_bytes = _make_zip({"MSSHI-Doctor.exe": b"stub-exe-v1.3.0"})
            expected_sha = hashlib.sha256(zip_bytes).hexdigest()
            release_payload = {
                "tag_name": "v1.3.0",
                "name": "MSSHI Doctor 1.3.0",
                "html_url": f"https://github.com/{APP_REPO_FULL_NAME}/releases/tag/v1.3.0",
                "body": "- Perbaikan self-update\n- Optimasi startup",
                "assets": [
                    {
                        "name": "MSSHI-Doctor-1.3.0-windows-x64.zip",
                        "size": len(zip_bytes),
                        "browser_download_url": f"https://github.com/{APP_REPO_FULL_NAME}/releases/download/v1.3.0/MSSHI-Doctor-1.3.0-windows-x64.zip",
                    },
                    {
                        "name": "SHA256SUMS.txt",
                        "size": 100,
                        "browser_download_url": f"https://github.com/{APP_REPO_FULL_NAME}/releases/download/v1.3.0/SHA256SUMS.txt",
                    },
                ],
            }
            calls: list[tuple[str, dict[str, str]]] = []

            def fake_http(url: str, headers: dict[str, str], _max_bytes: int):
                calls.append((url, dict(headers)))
                if url.endswith("SHA256SUMS.txt"):
                    return 200, {}, f"{expected_sha}  MSSHI-Doctor-1.3.0-windows-x64.zip\n".encode()
                if headers.get("If-None-Match") == '"etag-130"':
                    return 304, {"x-ratelimit-remaining": "58"}, b""
                return 200, {"etag": '"etag-130"', "x-ratelimit-remaining": "59"}, json.dumps(release_payload).encode()

            first = check_app_update(current_version="1.2.0", cache_dir=cache, http_get=fake_http)
            self.assertTrue(first["update_available"])
            self.assertEqual(first["connection_status"], "ONLINE_OK")
            self.assertEqual(first["asset"]["sha256"], expected_sha)
            self.assertEqual(first["support_url"], SAWERIA_URL)

            second = check_app_update(current_version="1.2.0", cache_dir=cache, http_get=fake_http)
            self.assertTrue(second["from_cache"])
            self.assertEqual(second["connection_status"], "ONLINE_ETAG_CACHED")
            self.assertTrue(second["update_available"])

    def test_offline_graceful_status(self) -> None:
        with TemporaryDirectory() as tmp:
            def offline_http(_url: str, _headers: dict[str, str], _max_bytes: int):
                raise URLError("network down")

            res = check_app_update(cache_dir=Path(tmp), http_get=offline_http)
            self.assertEqual(res["connection_status"], "OFFLINE")
            self.assertFalse(res["update_available"])
            self.assertEqual(res["exit_code"], 2)

    def test_staging_verifies_sha256_blocks_zip_slip_and_writes_handoff(self) -> None:
        with TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "app"
            app_dir.mkdir()
            (app_dir / "MSSHI-Doctor.exe").write_bytes(b"old-exe")

            good_zip = _make_zip({"MSSHI-Doctor.exe": b"new-exe", "README.md": b"v1.3.0"})
            good_sha = hashlib.sha256(good_zip).hexdigest()
            self.assertEqual(parse_sha256sums(f"{good_sha} *MSSHI-Doctor.zip", "MSSHI-Doctor.zip"), good_sha)

            with self.assertRaises(ValueError):
                stage_windows_self_update(
                    app_dir=app_dir,
                    target_version="1.3.0",
                    zip_bytes=good_zip,
                    expected_sha256="0" * 64,
                )

            evil_zip = _make_zip({"../escape.txt": b"boom"})
            with self.assertRaises(ValueError):
                stage_windows_self_update(
                    app_dir=app_dir,
                    target_version="1.3.0",
                    zip_bytes=evil_zip,
                )

            staged = stage_windows_self_update(
                app_dir=app_dir,
                target_version="1.3.0",
                zip_bytes=good_zip,
                expected_sha256=good_sha,
                current_pid=99999,
            )
            self.assertTrue(staged["sha256_verified"])
            self.assertEqual(staged["files_extracted"], 2)
            handoff = Path(staged["handoff_script"])
            self.assertTrue(handoff.is_file())
            self.assertIn("MSSHI-Doctor.exe", handoff.read_text(encoding="utf-8"))

    def test_run_self_update_dry_run_and_apply_portable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "portable"
            app_dir.mkdir()
            (app_dir / "MSSHI-Doctor.exe").write_bytes(b"v1.2.0")
            zip_bytes = _make_zip({"MSSHI-Doctor.exe": b"v1.3.0"})
            expected_sha = hashlib.sha256(zip_bytes).hexdigest()
            release_payload = {
                "tag_name": "v1.3.0",
                "name": "MSSHI Doctor 1.3.0",
                "html_url": f"https://github.com/{APP_REPO_FULL_NAME}/releases/tag/v1.3.0",
                "body": "Changelog test",
                "assets": [
                    {
                        "name": "MSSHI-Doctor-1.3.0-windows-x64.zip",
                        "size": len(zip_bytes),
                        "browser_download_url": f"https://github.com/{APP_REPO_FULL_NAME}/releases/download/v1.3.0/MSSHI-Doctor-1.3.0-windows-x64.zip",
                    },
                    {
                        "name": "SHA256SUMS.txt",
                        "size": 100,
                        "browser_download_url": f"https://github.com/{APP_REPO_FULL_NAME}/releases/download/v1.3.0/SHA256SUMS.txt",
                    },
                ],
            }

            def fake_http(url: str, _headers: dict[str, str], _max_bytes: int):
                if url.endswith(".zip"):
                    return 200, {}, zip_bytes
                if url.endswith("SHA256SUMS.txt"):
                    return 200, {}, f"{expected_sha}  MSSHI-Doctor-1.3.0-windows-x64.zip\n".encode()
                return 200, {}, json.dumps(release_payload).encode()

            preview = run_self_update(apply=False, app_dir=app_dir, cache_dir=root / "c", http_get=fake_http)
            self.assertTrue(preview["dry_run"])
            self.assertFalse(preview["applied"])

            applied = run_self_update(apply=True, app_dir=app_dir, cache_dir=root / "c", http_get=fake_http)
            self.assertTrue(applied["applied"])
            self.assertTrue(Path(applied["handoff_script"]).is_file())

    def test_cli_version_reports_1_2_0(self) -> None:
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            rc = run_cli(["--version", "--json"])
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["version"], APP_VERSION)


if __name__ == "__main__":
    unittest.main()
