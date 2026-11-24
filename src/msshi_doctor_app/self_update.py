"""Dedicated App Self-Update engine for MSSHI Doctor (zidanaisar624/MSSHI-DOCTOR).

Strictly separated from MSSHI-Core online repair:
- Only contacts allowlisted GitHub hosts for zidanaisar624/MSSHI-DOCTOR.
- Supports ETag caching, offline/rate-limit graceful reporting, changelog preview,
  SHA-256 verification, zip-slip protection, and 2-stage Windows self-replace handoff
  (because Windows locks a running .exe file).
- Zero paid LLM calls and zero access to Zone 2/3 user files.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile

from .constants import (
    ALLOWED_APP_UPDATE_HOSTS,
    APP_NAME,
    APP_REPO_FULL_NAME,
    APP_REPO_URL,
    APP_VERSION,
    EXIT_CORE_ERROR,
    EXIT_HEALTHY,
    EXIT_NEEDS_ATTENTION,
    SAWERIA_URL,
    resolve_cache_dir,
)
from .safety import atomic_write_bytes


def _atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))

MAX_UPDATE_META_BYTES = 2 * 1024 * 1024
MAX_UPDATE_ASSET_BYTES = 128 * 1024 * 1024
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
SHA256_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+)$")

HttpFetcher = Callable[[str, dict[str, str], int], tuple[int, dict[str, str], bytes]]


def parse_semver(raw: str | None) -> tuple[int, int, int] | None:
    if not raw:
        return None
    m = SEMVER_RE.match(raw.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_newer_version(candidate: str | None, current: str = APP_VERSION) -> bool:
    cand = parse_semver(candidate)
    curr = parse_semver(current)
    if cand is None or curr is None:
        return False
    return cand > curr


def validate_app_update_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"URL self-update harus HTTPS: {url}")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_APP_UPDATE_HOSTS:
        raise ValueError(f"Host self-update tidak diizinkan oleh allowlist: {host}")
    if host == "api.github.com" and not parsed.path.startswith(f"/repos/{APP_REPO_FULL_NAME}"):
        raise ValueError(f"Endpoint API harus khusus {APP_REPO_FULL_NAME}: {parsed.path}")
    if host == "github.com" and not parsed.path.startswith(f"/{APP_REPO_FULL_NAME}/"):
        raise ValueError(f"URL GitHub harus khusus {APP_REPO_FULL_NAME}: {parsed.path}")
    return url


def default_http_get(url: str, headers: dict[str, str], max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    validate_app_update_url(url)
    req_headers = {
        "User-Agent": f"MSSHI-Doctor/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
        **headers,
    }
    req = Request(url, headers=req_headers, method="GET")
    try:
        with urlopen(req, timeout=15) as resp:
            final_url = resp.geturl()
            validate_app_update_url(final_url)
            raw = resp.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError(f"Ukuran respons melebihi batas aman {max_bytes} bytes.")
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), resp_headers, raw
    except HTTPError as err:
        resp_headers = {k.lower(): v for k, v in (err.headers.items() if err.headers else [])}
        body = b""
        try:
            body = err.read(65536)
        except OSError:
            pass
        return int(err.code), resp_headers, body


def _cache_path(cache_dir: Path | None) -> Path | None:
    if cache_dir is None:
        return None
    return cache_dir / "app_self_update_cache.json"


def _load_meta_cache(cache_dir: Path | None) -> dict | None:
    path = _cache_path(cache_dir)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("repo") == APP_REPO_FULL_NAME:
            return data
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _save_meta_cache(cache_dir: Path | None, payload: dict) -> None:
    path = _cache_path(cache_dir)
    if path is None:
        return
    try:
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError:
        pass


def _select_windows_asset(assets: list[dict]) -> tuple[dict | None, dict | None]:
    zip_asset = None
    sums_asset = None
    for item in assets or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        low = name.lower()
        if low.endswith(".zip") and ("windows" in low or "win64" in low or "x64" in low or "doctor" in low):
            if zip_asset is None:
                zip_asset = item
        if "sha256" in low or low == "checksums.txt":
            sums_asset = item
    return zip_asset, sums_asset


def parse_sha256sums(text: str, target_filename: str) -> str | None:
    want = Path(target_filename).name.lower()
    for line in text.splitlines():
        m = SHA256_LINE_RE.match(line.strip())
        if m and Path(m.group(2).strip()).name.lower() == want:
            return m.group(1).lower()
    return None


def detect_install_mode(app_dir: Path | None = None) -> tuple[str, Path]:
    if getattr(sys, "frozen", False):
        return "portable_exe", Path(sys.executable).resolve().parent
    root = (app_dir or Path(__file__).resolve().parents[2]).resolve()
    if (root / "MSSHI-Doctor.exe").is_file() or (root / "_internal").is_dir():
        return "portable_exe", root
    return "source_checkout", root


def check_app_update(
    *,
    current_version: str = APP_VERSION,
    cache_dir: Path | None = None,
    http_get: HttpFetcher | None = None,
) -> dict:
    """Check https://github.com/zidanaisar624/MSSHI-DOCTOR for a newer release (read-only)."""
    fetcher = http_get or default_http_get
    effective_cache = cache_dir if cache_dir is not None else resolve_cache_dir()
    cached = _load_meta_cache(effective_cache)

    url = f"https://api.github.com/repos/{APP_REPO_FULL_NAME}/releases/latest"
    headers: dict[str, str] = {}
    if cached and cached.get("etag"):
        headers["If-None-Match"] = str(cached["etag"])

    release_obj: dict | None = None
    connection_status = "ONLINE_OK"
    from_cache = False
    error_message: str | None = None
    rate_remaining: int | None = None

    try:
        status, resp_headers, raw = fetcher(url, headers, MAX_UPDATE_META_BYTES)
        if "x-ratelimit-remaining" in resp_headers:
            try:
                rate_remaining = int(resp_headers["x-ratelimit-remaining"])
            except ValueError:
                rate_remaining = None

        if status == 304 and cached and isinstance(cached.get("release"), dict):
            release_obj = cached["release"]
            connection_status = "ONLINE_ETAG_CACHED"
            from_cache = True
        elif status == 200:
            release_obj = json.loads(raw.decode("utf-8"))
            _save_meta_cache(
                effective_cache,
                {
                    "repo": APP_REPO_FULL_NAME,
                    "etag": resp_headers.get("etag"),
                    "checked_utc": datetime.now(timezone.utc).isoformat(),
                    "release": release_obj,
                },
            )
        elif status == 404:
            # No formal GitHub Release published yet — fall back to /tags read-only
            tags_url = f"https://api.github.com/repos/{APP_REPO_FULL_NAME}/tags?per_page=10"
            t_status, _, t_raw = fetcher(tags_url, {}, MAX_UPDATE_META_BYTES)
            if t_status == 200:
                tags = json.loads(t_raw.decode("utf-8"))
                best_tag = None
                best_sv = None
                for t in tags if isinstance(tags, list) else []:
                    name = str(t.get("name") or "")
                    sv = parse_semver(name)
                    if sv and (best_sv is None or sv > best_sv):
                        best_sv = sv
                        best_tag = name
                if best_tag:
                    release_obj = {
                        "tag_name": best_tag,
                        "name": f"{APP_NAME} {best_tag}",
                        "html_url": f"{APP_REPO_URL}/releases/tag/{best_tag}",
                        "body": "Tag rilis ditemukan di GitHub (belum memiliki aset rilis terpisah).",
                        "assets": [],
                    }
                else:
                    connection_status = "NO_RELEASES_YET"
            else:
                connection_status = "RATE_LIMITED" if t_status == 403 else f"HTTP_{t_status}"
                error_message = f"GitHub mengembalikan status HTTP {t_status}."
        elif status in (403, 429) and (status == 429 or rate_remaining == 0 or "retry-after" in resp_headers):
            connection_status = "RATE_LIMITED"
            error_message = "Batas permintaan GitHub API tercapai. Coba lagi beberapa menit lagi."
        else:
            connection_status = f"HTTP_{status}"
            error_message = f"GitHub mengembalikan status HTTP {status}."
    except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        connection_status = "OFFLINE"
        error_message = str(exc)

    latest_tag = str(release_obj.get("tag_name") or "").strip() if release_obj else ""
    latest_version = latest_tag.lstrip("v") if latest_tag else current_version
    update_available = bool(release_obj and is_newer_version(latest_tag, current_version))
    zip_asset, sums_asset = _select_windows_asset(release_obj.get("assets", []) if release_obj else [])

    expected_sha256: str | None = None
    if zip_asset and sums_asset and sums_asset.get("browser_download_url"):
        try:
            s_code, _, s_raw = fetcher(str(sums_asset["browser_download_url"]), {}, 262144)
            if s_code == 200:
                expected_sha256 = parse_sha256sums(s_raw.decode("utf-8", errors="replace"), str(zip_asset["name"]))
        except (URLError, OSError, ValueError):
            expected_sha256 = None

    install_mode, app_root = detect_install_mode()
    return {
        "schema_version": 1,
        "operation": "app_self_update_check",
        "app": APP_NAME,
        "app_repo": APP_REPO_FULL_NAME,
        "app_repo_url": APP_REPO_URL,
        "support_url": SAWERIA_URL,
        "current_version": current_version,
        "latest_version": latest_version,
        "latest_tag": latest_tag or f"v{current_version}",
        "update_available": update_available,
        "install_mode": install_mode,
        "app_root": str(app_root),
        "connection_status": connection_status,
        "from_cache": from_cache,
        "rate_limit_remaining": rate_remaining,
        "release_name": (release_obj or {}).get("name") or f"{APP_NAME} v{latest_version}",
        "release_url": (release_obj or {}).get("html_url") or f"{APP_REPO_URL}/releases",
        "changelog": ((release_obj or {}).get("body") or "").strip()[:4000],
        "asset": {
            "name": zip_asset.get("name"),
            "url": zip_asset.get("browser_download_url"),
            "size": zip_asset.get("size"),
            "sha256": expected_sha256,
        } if zip_asset else None,
        "error_message": error_message,
        "exit_code": EXIT_HEALTHY if connection_status in ("ONLINE_OK", "ONLINE_ETAG_CACHED", "NO_RELEASES_YET") else EXIT_NEEDS_ATTENTION,
    }


def _safe_extract_zip(zip_bytes: bytes, dest_dir: Path) -> list[str]:
    extracted: list[str] = []
    dest_resolved = dest_dir.resolve()
    dest_resolved.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.startswith("/") or ".." in Path(name).parts or ":" in name:
                raise ValueError(f"Entri ZIP berbahaya ditolak (zip-slip): {info.filename}")
            target = (dest_resolved / name).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError as exc:
                raise ValueError(f"Entri ZIP keluar dari folder staging: {info.filename}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            data = zf.read(info)
            atomic_write_bytes(target, data)
            extracted.append(name)
    return extracted


def stage_windows_self_update(
    *,
    app_dir: Path,
    target_version: str,
    zip_bytes: bytes,
    expected_sha256: str | None = None,
    current_pid: int | None = None,
) -> dict:
    """Verify SHA-256, extract safely to sibling staging folder, and write 2-stage Windows swap script."""
    actual_sha256 = hashlib.sha256(zip_bytes).hexdigest().lower()
    if expected_sha256 and actual_sha256 != expected_sha256.strip().lower():
        raise ValueError(
            f"SHA-256 paket update tidak cocok! Diharapkan {expected_sha256}, didapat {actual_sha256}."
        )

    safe_ver = re.sub(r"[^A-Za-z0-9._-]", "_", target_version.strip().lstrip("v") or "next")
    staging_root = app_dir.resolve() / ".update-staging" / f"v{safe_ver}"
    extracted = _safe_extract_zip(zip_bytes, staging_root)
    copy_src = staging_root
    top_entries = [p for p in staging_root.iterdir()] if staging_root.is_dir() else []
    if len(top_entries) == 1 and top_entries[0].is_dir() and (top_entries[0] / "MSSHI-Doctor.exe").is_file():
        copy_src = top_entries[0]

    pid = current_pid if current_pid is not None else os.getpid()
    backup_dir = app_dir.resolve() / ".update-backup" / f"before-v{safe_ver}"
    handoff_script = app_dir.resolve() / ".update-staging" / f"apply_doctor_update_v{safe_ver}.cmd"
    cmd_content = (
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        "echo [MSSHI Doctor Self-Update] Menunggu proses lama selesai...\r\n"
        f":wait_pid\r\n"
        f"tasklist /FI \"PID eq {pid}\" 2>nul | find \"{pid}\" >nul\r\n"
        "if not errorlevel 1 (\r\n"
        "  timeout /t 1 /nobreak >nul\r\n"
        "  goto wait_pid\r\n"
        ")\r\n"
        f"if not exist \"{backup_dir}\" mkdir \"{backup_dir}\"\r\n"
        f"if exist \"{app_dir.resolve()}\\MSSHI-Doctor.exe\" copy /Y \"{app_dir.resolve()}\\MSSHI-Doctor.exe\" \"{backup_dir}\\MSSHI-Doctor.exe\" >nul\r\n"
        f"xcopy /E /I /Y \"{copy_src}\\*\" \"{app_dir.resolve()}\\\" >nul\r\n"
        "if errorlevel 1 (\r\n"
        "  echo [ERROR] Gagal menyalin file update. Backup tersimpan di folder .update-backup.\r\n"
        "  exit /b 1\r\n"
        ")\r\n"
        f"echo [OK] MSSHI Doctor v{safe_ver} siap dijalankan.\r\n"
        f"if exist \"{app_dir.resolve()}\\MSSHI-Doctor.exe\" start \"\" \"{app_dir.resolve()}\\MSSHI-Doctor.exe\"\r\n"
    )
    _atomic_write_text(handoff_script, cmd_content)
    return {
        "schema_version": 1,
        "operation": "app_self_update_staged",
        "target_version": safe_ver,
        "sha256": actual_sha256,
        "sha256_verified": bool(expected_sha256),
        "staging_dir": str(staging_root),
        "backup_dir": str(backup_dir),
        "handoff_script": str(handoff_script),
        "files_extracted": len(extracted),
        "exit_code": EXIT_HEALTHY,
    }


def run_self_update(
    *,
    apply: bool = False,
    app_dir: Path | None = None,
    cache_dir: Path | None = None,
    http_get: HttpFetcher | None = None,
) -> dict:
    """Preview (--dry-run) or stage (--apply) MSSHI Doctor self-update safely."""
    check = check_app_update(cache_dir=cache_dir, http_get=http_get)
    mode, resolved_root = detect_install_mode(app_dir)
    if not check["update_available"]:
        return {
            **check,
            "operation": "app_self_update",
            "dry_run": not apply,
            "applied": False,
            "note": f"MSSHI Doctor sudah berada di versi terbaru (v{check['current_version']}).",
        }
    if not apply:
        return {
            **check,
            "operation": "app_self_update_preview",
            "dry_run": True,
            "applied": False,
            "install_mode": mode,
            "app_root": str(resolved_root),
            "note": (
                "Mode portabel siap di-stage dengan --apply (menghasilkan skrip swap 2-tahap aman)."
                if mode == "portable_exe" and check.get("asset")
                else f"Jalankan 'git pull --ff-only' di {resolved_root} atau unduh paket rilis dari {check['release_url']}."
            ),
            "exit_code": EXIT_NEEDS_ATTENTION,
        }

    asset = check.get("asset")
    if mode != "portable_exe" or not asset or not asset.get("url"):
        return {
            **check,
            "operation": "app_self_update",
            "dry_run": False,
            "applied": False,
            "note": (
                f"Instalasi mode sumber ({mode}) tidak menimpa file kerja otomatis. "
                f"Perbarui via 'git pull --ff-only' atau buka {check['release_url']}."
            ),
            "exit_code": EXIT_NEEDS_ATTENTION,
        }

    fetcher = http_get or default_http_get
    dl_url = validate_app_update_url(str(asset["url"]))
    status, _, zip_bytes = fetcher(dl_url, {}, MAX_UPDATE_ASSET_BYTES)
    if status != 200:
        return {
            **check,
            "operation": "app_self_update",
            "dry_run": False,
            "applied": False,
            "error_message": f"Gagal mengunduh paket rilis (HTTP {status}).",
            "exit_code": EXIT_CORE_ERROR,
        }
    staged = stage_windows_self_update(
        app_dir=resolved_root,
        target_version=check["latest_version"],
        zip_bytes=zip_bytes,
        expected_sha256=asset.get("sha256"),
    )
    return {
        **check,
        **staged,
        "dry_run": False,
        "applied": True,
        "note": "Paket update berhasil diverifikasi & di-stage. Tutup aplikasi lalu jalankan handoff_script untuk swap aman.",
    }
