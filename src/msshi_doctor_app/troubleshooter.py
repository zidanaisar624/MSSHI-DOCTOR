"""Quick Troubleshooter for recurring Hermes & MSSHI Windows issues (read-only checks + guided fixes)."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shutil
import subprocess

from .local_probe import (
    _hidden_subprocess_kwargs,
    _read_windows_hkcu_env,
    find_msshi_executable,
    resolve_hermes_home,
    resolve_msshi_home,
)
from .safety import is_reparse_or_symlink, is_zone2_user_owned, is_zone3_protected, reject_links

HEAVY_SKILL_DIRS = frozenset({".git", "node_modules", "venv", ".venv", "__pycache__", "site-packages"})
HEAVY_FILE_THRESHOLD_BYTES = 512 * 1024  # 512 KiB
LOG_TAIL_BYTES = 128 * 1024


def _allowed_path(path: Path) -> bool:
    if is_zone3_protected(path):
        return False
    try:
        reject_links(path)
        return True
    except (OSError, ValueError):
        return False


def _boolean_preference(path: Path, field: str) -> bool | None:
    if not _allowed_path(path) or not path.is_file():
        return None
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return None
        doc = json.loads(raw.decode("utf-8-sig"))
        value = doc.get(field) if isinstance(doc, dict) else None
        return value if isinstance(value, bool) else None
    except (OSError, ValueError):
        return None


def check_grey_screen(hermes_home: Path) -> dict:
    pref_path = hermes_home / "tools/msshi/desktop-preferences.json"
    sw_enabled = _boolean_preference(pref_path, "software_rendering")
    desktop_exe = hermes_home / "hermes-agent/apps/desktop/release/win-unpacked/Hermes.exe"
    return {
        "id": "desktop_grey_screen",
        "title": "Hermes Desktop Abu-Abu (Grey Screen)",
        "status": "INFO" if sw_enabled else "READY_ACTION",
        "summary": (
            "Mode software rendering sudah tercatat aktif pada desktop-preferences.json."
            if sw_enabled
            else "Gunakan software rendering resmi Hermes (HERMES_DESKTOP_DISABLE_GPU=1) tanpa mengubah Chrome."
        ),
        "details": {
            "desktop_executable_present": _allowed_path(desktop_exe) and desktop_exe.is_file(),
            "software_rendering_preference": sw_enabled,
        },
        "recommended_command": f'msshi desktop --hermes-home "{hermes_home}" --software-rendering',
        "action_id": "launch_software_rendering",
        "action_label": "Buka Hermes (Software Rendering)",
    }


def check_skills_timeout(hermes_home: Path) -> dict:
    """Scan active profile skills/ for heavy directories/files without touching Zone 3."""
    findings: list[dict] = []
    roots = [("default", hermes_home / "skills")]
    p_dir = hermes_home / "profiles"
    roots_checked = 0
    scan_errors = []
    if _allowed_path(p_dir) and p_dir.is_dir():
        try:
            for entry in p_dir.iterdir():
                deleted = p_dir / ".deleted" / entry.name
                if (not entry.name.startswith(".") and _allowed_path(entry) and entry.is_dir()
                        and _allowed_path(deleted) and not deleted.exists()):
                    roots.append((entry.name, entry / "skills"))
        except OSError as exc:
            scan_errors.append(exc)

    for pid, s_root in roots:
        if not _allowed_path(s_root) or not s_root.is_dir():
            continue
        roots_checked += 1
        for dirpath, dirnames, filenames in os.walk(s_root, followlinks=False, onerror=scan_errors.append):
            cur = Path(dirpath)
            for d in list(dirnames):
                if is_zone3_protected(cur / d) or is_reparse_or_symlink(cur / d):
                    dirnames.remove(d)
                elif d.lower() in HEAVY_SKILL_DIRS:
                    findings.append({
                        "profile": pid,
                        "kind": "heavy_dir",
                        "path": str(cur / d),
                    })
                    dirnames.remove(d)
            for fname in filenames:
                fpath = cur / fname
                if is_zone3_protected(fpath) or is_zone2_user_owned(fpath) or is_reparse_or_symlink(fpath):
                    continue
                try:
                    size = fpath.stat().st_size
                except OSError:
                    continue
                if size > HEAVY_FILE_THRESHOLD_BYTES:
                    findings.append({
                        "profile": pid,
                        "kind": "large_file",
                        "path": str(fpath),
                        "size_kb": round(size / 1024, 1),
                    })
            if len(findings) >= 40:
                break

    return {
        "id": "skills_panel_timeout",
        "title": "Panel Skills Timeout / Startup Berat",
        "status": "WARN" if findings else ("UNKNOWN" if not roots_checked or scan_errors else "OK"),
        "summary": (
            f"Ditemukan {len(findings)} folder/file berat di dalam folder skills/ yang bisa memperlambat scanner."
            if findings
            else ("Belum ada hasil lengkap dari folder skill yang dapat dibaca." if not roots_checked or scan_errors
                  else "Folder skill yang diperiksa bebas dari cache berat dan file di atas 512 KiB.")
        ),
        "details": {"findings_count": len(findings), "samples": findings[:10],
                    "roots_checked": roots_checked, "scan_errors": len(scan_errors)},
        "recommended_command": f'msshi audit --home "{hermes_home}"',
        "action_id": "copy_command",
        "action_label": "Salin Perintah Audit",
    }


def check_gateway_health(hermes_home: Path) -> dict:
    """Inspect sanitized log counters & gateway lock files without exposing raw log lines."""
    logs_dir = hermes_home / "logs"
    stats: dict[str, dict] = {}
    for fname in ("gateway.log", "desktop.log", "msshi-desktop.log"):
        fpath = logs_dir / fname
        if not _allowed_path(fpath) or not fpath.is_file():
            continue
        try:
            with fpath.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                start = max(0, size - LOG_TAIL_BYTES)
                stream.seek(start)
                raw = stream.read(LOG_TAIL_BYTES)
            if start:
                _, _, raw = raw.partition(b"\n")
            recent = [line.lower() for line in raw.splitlines()[-300:] if line.strip()]
            stats[fname] = {
                "size_kb": round(size / 1024, 1),
                "sampled_lines": len(recent),
                "recent_errors": sum(b"error" in line for line in recent),
                "recent_timeouts": sum(b"timeout" in line or b"timed out" in line for line in recent),
                "recent_unclean": sum(b"unclean" in line for line in recent),
            }
        except OSError:
            continue

    lock_path = logs_dir / ".__gateway.lock"
    lock_present = lock_path.exists() if _allowed_path(lock_path) else None
    recent_err_total = sum(v.get("recent_errors", 0) for v in stats.values())
    recent_to_total = sum(v.get("recent_timeouts", 0) for v in stats.values())
    unclean = sum(v["recent_unclean"] for v in stats.values())
    sampled = sum(v["sampled_lines"] for v in stats.values())
    status = "UNKNOWN" if not sampled else ("WARN" if recent_err_total or recent_to_total or unclean else "OK")
    return {
        "id": "gateway_stuck",
        "title": "Gateway Macet / Startup Lambat",
        "status": status,
        "summary": (
            f"Dari {sampled} baris log yang diperiksa, {recent_err_total} memuat error, "
            f"{recent_to_total} timeout, dan {unclean} unclean. Gunakan pemeriksaan gateway untuk status saat ini."
            if sampled else "Log gateway kosong, belum tersedia, atau tidak dapat dibaca. Jalankan pemeriksaan gateway."
        ),
        "details": {"logs": stats, "gateway_lock_present": lock_present},
        "recommended_command": f'msshi desktop --hermes-home "{hermes_home}" --check-only',
        "action_id": "copy_command",
        "action_label": "Salin Perintah Prewarm Gateway",
    }


def _running_lightcraft() -> bool | None:
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=8, **_hidden_subprocess_kwargs())
        if result.returncode:
            return None
        images = [row[0].casefold() for row in csv.reader(result.stdout.splitlines())
                  if len(row) >= 2 and row[1].strip().isdigit()]
        return "lightcraft.exe" in images if images else None
    except (OSError, subprocess.SubprocessError, csv.Error):
        return None


def check_lightcraft_lock(hermes_home: Path) -> dict:
    """Report observed startup policy and process independently; neither proves a file lock."""
    running_lightcraft = _running_lightcraft()
    policy_path = hermes_home / "msshi-runtime/policy.json"
    auto_open = _boolean_preference(policy_path, "auto_open_on_boot")
    policy = {True: "Kebijakan mengizinkan aplikasi terbuka saat startup (auto_open_on_boot=true).",
              False: "Kebijakan menonaktifkan pembukaan saat startup (auto_open_on_boot=false).",
              None: "Nilai auto_open_on_boot belum dapat diverifikasi."}
    process = {True: "Proses Lightcraft sedang aktif. Jika library terkunci, simpan pekerjaan lalu tutup jendelanya.",
               False: "Proses Lightcraft.exe tidak terdeteksi.", None: "Status proses Lightcraft belum dapat diperiksa."}
    status = "WARN" if running_lightcraft or auto_open else ("UNKNOWN" if None in (running_lightcraft, auto_open) else "OK")
    return {
        "id": "lightcraft_lock",
        "title": "Lightcraft Terkunci / Aplikasi Terbuka Sendiri",
        "status": status,
        "summary": f"{policy[auto_open]} {process[running_lightcraft]}",
        "details": {
            "lightcraft_process_active": running_lightcraft,
            "auto_open_on_boot": auto_open,
        },
        "recommended_command": f'msshi runtime status --home "{hermes_home}" --json',
        "action_id": "copy_command",
        "action_label": "Salin Perintah Status Runtime",
    }


def check_msshi_path(msshi_home: str | Path | None = None) -> dict:
    """Verify whether `msshi` command is resolvable on PATH and inspect launcher location."""
    which_msshi = shutil.which("msshi")
    bin_dir = resolve_msshi_home(msshi_home) / "bin"
    shim = bin_dir / ("msshi.cmd" if os.name == "nt" else "msshi")
    user_path = _read_windows_hkcu_env("Path") or "" if os.name == "nt" else os.environ.get("PATH", "")
    in_user_path = str(bin_dir).lower() in user_path.lower()

    explicitly_scoped = bool(msshi_home or os.environ.get("MSSHI_HOME"))
    if which_msshi and (not explicitly_scoped or Path(which_msshi).absolute() == shim.absolute()):
        status = "OK"
        summary = f"Perintah `msshi` terdeteksi pada PATH: {which_msshi}"
    elif which_msshi:
        status = "WARN"
        summary = f"PATH mengarah ke instalasi lain. Lokasi launcher yang dipilih: {bin_dir}."
    elif shim.is_file():
        status = "WARN"
        summary = f"Launcher ditemukan di {shim}, tetapi belum terbaca di PATH sesi ini. Buka terminal baru atau jalankan bootstrap."
    else:
        status = "WARN"
        summary = f"Launcher global `msshi` belum ditemukan di {bin_dir}."

    return {
        "id": "msshi_path",
        "title": "PATH Perintah `msshi` Tidak Terbaca",
        "status": status,
        "summary": summary,
        "details": {
            "which_msshi": which_msshi,
            "expected_shim": str(shim),
            "shim_exists": shim.is_file(),
            "in_user_path_env": in_user_path,
        },
        "recommended_command": "Get-Command msshi; msshi doctor --json",
        "action_id": "copy_command",
        "action_label": "Salin Perintah Cek PATH",
    }


def run_all_troubleshooter_checks(
    *,
    hermes_home: str | Path | None = None,
    msshi_home: str | Path | None = None,
) -> dict:
    h_home = resolve_hermes_home(hermes_home)
    m_home = resolve_msshi_home(msshi_home)
    checks = [
        check_grey_screen(h_home),
        check_skills_timeout(h_home),
        check_gateway_health(h_home),
        check_lightcraft_lock(h_home),
        check_msshi_path(msshi_home),
    ]
    warn_count = sum(1 for c in checks if c["status"] == "WARN")
    unknown_count = sum(1 for c in checks if c["status"] == "UNKNOWN")
    return {
        "schema_version": 1,
        "read_only": True,
        "model_calls": 0,
        "hermes_home": str(h_home),
        "msshi_home": str(m_home),
        "warnings": warn_count,
        "unknown_checks": unknown_count,
        "checks": checks,
        "status": "NEEDS_ATTENTION" if warn_count else ("INCOMPLETE" if unknown_count else "HEALTHY"),
    }


def launch_hermes_software_rendering(
    *,
    hermes_home: str | Path | None = None,
    msshi_home: str | Path | None = None,
) -> dict:
    """Explicit user-triggered helper to launch Hermes Desktop with software rendering."""
    exe = find_msshi_executable(msshi_home)
    if not exe:
        return {"ok": False, "error": "msshi CLI tidak ditemukan."}
    h_home = resolve_hermes_home(hermes_home)
    cmd = [*exe, "desktop", "--hermes-home", str(h_home), "--software-rendering"]
    env = {**os.environ, "HERMES_HOME": str(h_home)}
    if msshi_home is not None:
        env["MSSHI_HOME"] = str(resolve_msshi_home(msshi_home))
    try:
        subprocess.Popen(cmd, env=env, **_hidden_subprocess_kwargs())
        return {"ok": True, "command": " ".join(cmd)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
