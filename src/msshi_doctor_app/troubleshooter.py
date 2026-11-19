"""Quick Troubleshooter for recurring Hermes & MSSHI Windows issues (read-only checks + guided fixes)."""
from __future__ import annotations

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
from .safety import is_reparse_or_symlink, is_zone3_protected

HEAVY_SKILL_DIRS = frozenset({".git", "node_modules", "venv", ".venv", "__pycache__", "site-packages"})
HEAVY_FILE_THRESHOLD_BYTES = 512 * 1024  # 512 KiB


def check_grey_screen(hermes_home: Path) -> dict:
    pref_path = hermes_home / "tools/msshi/desktop-preferences.json"
    sw_enabled = False
    if pref_path.is_file():
        try:
            doc = json.loads(pref_path.read_text(encoding="utf-8-sig"))
            sw_enabled = bool(isinstance(doc, dict) and doc.get("software_rendering") is True)
        except (OSError, ValueError):
            pass
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
            "desktop_executable_present": desktop_exe.is_file(),
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
    if p_dir.is_dir():
        for entry in sorted(p_dir.iterdir()):
            if entry.is_dir() and not entry.name.startswith(".") and not (p_dir / ".deleted" / entry.name).exists():
                roots.append((entry.name, entry / "skills"))

    for pid, s_root in roots:
        if not s_root.is_dir() or is_reparse_or_symlink(s_root) or is_zone3_protected(s_root):
            continue
        for dirpath, dirnames, filenames in os.walk(s_root, followlinks=False):
            cur = Path(dirpath)
            for d in list(dirnames):
                if d.lower() in HEAVY_SKILL_DIRS:
                    findings.append({
                        "profile": pid,
                        "kind": "heavy_dir",
                        "path": str(cur / d),
                    })
                    dirnames.remove(d)
                elif is_reparse_or_symlink(cur / d) or is_zone3_protected(cur / d):
                    dirnames.remove(d)
            for fname in filenames:
                fpath = cur / fname
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
        "status": "WARN" if findings else "OK",
        "summary": (
            f"Ditemukan {len(findings)} folder/file berat di dalam folder skills/ yang bisa memperlambat scanner."
            if findings
            else "Tree skills/ bersih dari .git, node_modules, venv, __pycache__, dan file > 512 KB."
        ),
        "details": {"findings_count": len(findings), "samples": findings[:10]},
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
        if not fpath.is_file():
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            recent = lines[-300:]
            stats[fname] = {
                "size_kb": round(fpath.stat().st_size / 1024, 1),
                "total_lines": len(lines),
                "recent_errors": sum(1 for ln in recent if "error" in ln.lower()),
                "recent_timeouts": sum(1 for ln in recent if "timeout" in ln.lower()),
                "recent_unclean": sum(1 for ln in recent if "unclean" in ln.lower()),
            }
        except OSError:
            continue

    lock_present = (logs_dir / ".__gateway.lock").exists()
    recent_err_total = sum(v.get("recent_errors", 0) for v in stats.values())
    recent_to_total = sum(v.get("recent_timeouts", 0) for v in stats.values())
    status = "WARN" if (recent_err_total > 10 or recent_to_total > 10) else "OK"
    return {
        "id": "gateway_stuck",
        "title": "Gateway Macet / Startup Lambat",
        "status": status,
        "summary": (
            f"Log terbaru (300 baris terakhir): {recent_err_total} error, {recent_to_total} timeout. "
            f"Lock marker: {'ada' if lock_present else 'tidak ada'}. Jangan hapus state.db."
        ),
        "details": {"logs": stats, "gateway_lock_present": lock_present},
        "recommended_command": f'msshi desktop --hermes-home "{hermes_home}" --check-only',
        "action_id": "copy_command",
        "action_label": "Salin Perintah Prewarm Gateway",
    }


def check_lightcraft_lock(hermes_home: Path) -> dict:
    """Check Lightcraft route status and whether Lightcraft GUI process is active (read-only)."""
    running_lightcraft = False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                **_hidden_subprocess_kwargs(),
            ).stdout.lower()
            running_lightcraft = "lightcraft" in out
        except (OSError, subprocess.SubprocessError):
            pass
    policy_path = hermes_home / "msshi-runtime/policy.json"
    auto_open = False
    if policy_path.is_file():
        try:
            auto_open = bool(json.loads(policy_path.read_text(encoding="utf-8-sig")).get("auto_open_on_boot"))
        except (OSError, ValueError):
            pass

    status = "WARN" if (running_lightcraft or auto_open) else "OK"
    return {
        "id": "lightcraft_lock",
        "title": "Lightcraft Terkunci / Aplikasi Terbuka Sendiri",
        "status": status,
        "summary": (
            "Proses Lightcraft terdeteksi aktif. Simpan & tutup GUI secara normal sebelum menjalankan tugas CLI."
            if running_lightcraft
            else "Kebijakan auto_open_on_boot=false aktif; tidak ada proses Lightcraft yang mengunci library."
        ),
        "details": {
            "lightcraft_process_active": running_lightcraft,
            "auto_open_on_boot": auto_open,
        },
        "recommended_command": f'msshi runtime status --home "{hermes_home}" --json',
        "action_id": "copy_command",
        "action_label": "Salin Perintah Status Runtime",
    }


def check_msshi_path(msshi_home: Path) -> dict:
    """Verify whether `msshi` command is resolvable on PATH and inspect launcher location."""
    which_msshi = shutil.which("msshi")
    bin_dir = msshi_home / "bin"
    shim = bin_dir / ("msshi.cmd" if os.name == "nt" else "msshi")
    user_path = _read_windows_hkcu_env("Path") or "" if os.name == "nt" else os.environ.get("PATH", "")
    in_user_path = str(bin_dir).lower() in user_path.lower()

    if which_msshi:
        status = "OK"
        summary = f"Perintah `msshi` terdeteksi pada PATH: {which_msshi}"
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
        check_msshi_path(m_home),
    ]
    warn_count = sum(1 for c in checks if c["status"] == "WARN")
    return {
        "schema_version": 1,
        "read_only": True,
        "model_calls": 0,
        "hermes_home": str(h_home),
        "msshi_home": str(m_home),
        "warnings": warn_count,
        "checks": checks,
        "status": "NEEDS_ATTENTION" if warn_count else "HEALTHY",
    }


def launch_hermes_software_rendering(
    *,
    hermes_home: str | Path | None = None,
    msshi_home: str | Path | None = None,
) -> dict:
    """Explicit user-triggered helper to launch Hermes Desktop with software rendering."""
    exe = find_msshi_executable(resolve_msshi_home(msshi_home))
    if not exe:
        return {"ok": False, "error": "msshi CLI tidak ditemukan."}
    h_home = resolve_hermes_home(hermes_home)
    cmd = [*exe, "desktop", "--hermes-home", str(h_home), "--software-rendering"]
    try:
        subprocess.Popen(cmd, **_hidden_subprocess_kwargs())
        return {"ok": True, "command": " ".join(cmd)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
