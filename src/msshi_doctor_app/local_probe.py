"""Read-only local ecosystem discovery, CLI bridge, hardware telemetry, and 5-status model."""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from .constants import DEFAULT_REF, EXIT_CORE_ERROR, EXIT_NEEDS_ATTENTION, PROFILE_ID_RE
from .safety import check_soul_health_passive, is_zone3_protected, reject_links

RELEASE_ID_RE = re.compile(r"^(?:[0-9a-f]{24}|[0-9a-f]{64})$")


def _hidden_subprocess_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def _read_windows_hkcu_env(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
        cleaned = str(value or "").strip()
        return cleaned or None
    except OSError:
        return None


def resolve_msshi_home(explicit: str | Path | None = None) -> Path:
    """Resolve global MSSHI launcher/state directory adaptively (C: or D:)."""
    if explicit:
        return Path(explicit).expanduser().absolute()
    if env_val := (os.environ.get("MSSHI_HOME") or "").strip():
        return Path(env_val).expanduser().absolute()
    if reg_val := _read_windows_hkcu_env("MSSHI_HOME"):
        return Path(reg_val).expanduser().absolute()
    candidates: list[Path] = []
    if os.name == "nt":
        if Path("D:/MSSHI/state.json").is_file():
            candidates.append(Path("D:/MSSHI"))
        local_app = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        candidates.append(Path(local_app) / "MSSHI")
        if Path("D:/").exists():
            candidates.append(Path("D:/MSSHI"))
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
        candidates.append(Path(xdg) / "msshi")
    for cand in candidates:
        if (cand / "state.json").is_file():
            return cand.absolute()
    return candidates[0].absolute()


def resolve_hermes_home(explicit: str | Path | None = None) -> Path:
    """Resolve Hermes home adaptively across Drive D: and Drive C:-only laptops."""
    if explicit:
        return Path(explicit).expanduser().absolute()
    if env_val := (os.environ.get("HERMES_HOME") or "").strip():
        return Path(env_val).expanduser().absolute()
    if reg_val := _read_windows_hkcu_env("HERMES_HOME"):
        return Path(reg_val).expanduser().absolute()
    candidates: list[Path] = []
    if os.name == "nt":
        if Path("D:/HermesData").is_dir():
            candidates.append(Path("D:/HermesData"))
        candidates.append(Path.home() / "HermesData")
        local_app = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        candidates.append(Path(local_app) / "hermes")
    candidates.append(Path.home() / ".hermes")
    for cand in candidates:
        if any((cand / m).exists() for m in ("config.yaml", "profiles", "msshi-runtime", "state.db")):
            return cand.absolute()
    return candidates[0].absolute()


def read_cli_state(msshi_home: Path | None = None) -> dict:
    """Read state.json to identify the active CLI release without guessing folder names."""
    home = resolve_msshi_home(msshi_home)
    state_path = home / "state.json"
    info = {
        "msshi_home": str(home),
        "state_path": str(state_path),
        "state_present": state_path.is_file(),
        "current_release_id": None,
        "active_release_dir": None,
        "git_commit": None,
        "managed_targets_count": 0,
        "managed_bindings_count": 0,
    }
    if not state_path.is_file():
        return info
    try:
        reject_links(state_path)
        data = json.loads(state_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Invalid launcher state document")
    except (OSError, ValueError):
        info["error"] = "unreadable_state_json"
        return info
    release_id = data.get("current")
    if isinstance(release_id, str) and RELEASE_ID_RE.fullmatch(release_id):
        info["current_release_id"] = release_id
        rel_dir = home / "releases" / release_id
        if rel_dir.is_dir():
            info["active_release_dir"] = str(rel_dir)
    info["git_commit"] = data.get("git_commit")
    targets = data.get("targets")
    if isinstance(targets, dict):
        info["managed_targets_count"] = len(targets)
    bindings = data.get("managed_bindings")
    if isinstance(bindings, list):
        info["managed_bindings_count"] = len(bindings)
    return info


def resolve_msshi_target_root(
    explicit_root: str | Path | None = None,
    *,
    msshi_home: Path | None = None,
    hermes_home: Path | None = None,
) -> tuple[Path | None, str]:
    """Identify the primary local MSSHI installation root to audit/repair."""
    if explicit_root:
        p = Path(explicit_root).expanduser().absolute()
        return p, "explicit_target_root"
    state = read_cli_state(msshi_home)
    if state.get("active_release_dir"):
        return Path(state["active_release_dir"]), "active_cli_release"
    h_home = resolve_hermes_home(hermes_home)
    upstream = h_home / "msshi-upstream"
    if (upstream / "VERSION.json").is_file():
        return upstream.absolute(), "hermes_msshi_upstream"
    return None, "not_found"


def detect_target_ref(target_root: Path | None) -> str:
    """Detect GitHub target ref (e.g. 'v3.1.2') from local VERSION.json, defaulting safely."""
    if target_root and (target_root / "VERSION.json").is_file():
        try:
            doc = json.loads(reject_links(target_root / "VERSION.json").read_text(encoding="utf-8-sig"))
            if not isinstance(doc, dict):
                return DEFAULT_REF
            ver = str(doc.get("version") or "").strip()
            if re.fullmatch(r"\d+\.\d+\.\d+", ver):
                return f"v{ver}"
        except (OSError, ValueError):
            pass
    return DEFAULT_REF


def find_msshi_executable(msshi_home: str | Path | None = None) -> list[str] | None:
    """Prefer the chosen installation. Only auto-discovery may fall back to PATH."""
    home = resolve_msshi_home(msshi_home)
    if os.name == "nt":
        cmd = home / "bin/msshi.cmd"
        if cmd.is_file():
            return [str(cmd)]
    else:
        shim = home / "bin/msshi"
        if shim.is_file():
            return [str(shim)]
    launcher = home / "launch.py"
    if launcher.is_file():
        return [sys.executable, "-B", "-X", "utf8", str(launcher)]
    if msshi_home is None and not os.environ.get("MSSHI_HOME"):
        found = shutil.which("msshi")
        if found:
            return [found]
    return None


def _run_msshi_json(
    args: list[str], *, msshi_home: str | Path | None = None,
    hermes_home: str | Path | None = None, timeout: float = 25.0,
) -> dict:
    """Run the chosen MSSHI installation with its selected Hermes profile root."""
    exe = find_msshi_executable(msshi_home)
    if not exe:
        return {"ok": False, "exit_code": None, "error": "msshi CLI tidak ditemukan pada PATH atau MSSHI_HOME."}
    cmd = [*exe, *args]
    env = os.environ.copy()
    if msshi_home is not None:
        env["MSSHI_HOME"] = str(resolve_msshi_home(msshi_home))
    if hermes_home is not None:
        env["HERMES_HOME"] = str(resolve_hermes_home(hermes_home))
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "exit_code": None, "error": f"{type(exc).__name__}: {exc}"}
    raw = (proc.stdout or "").strip()
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            # Handle trailing non-JSON lines if any
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                try:
                    parsed = json.loads(raw[start : end + 1])
                except ValueError:
                    parsed = None
    return {
        "ok": parsed is not None,
        "exit_code": proc.returncode,
        "data": parsed,
        "stderr": (proc.stderr or "").strip()[:500] if proc.stderr else "",
    }


def collect_hardware_telemetry(hermes_home: Path) -> dict:
    """Collect RAM, CPU, OS, and C:/D: disk telemetry without external dependencies."""
    total_bytes = 0
    avail_bytes = 0
    mem_load_pct = None
    if os.name == "nt":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        mem = MemoryStatusEx()
        mem.dwLength = ctypes.sizeof(mem)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            total_bytes = mem.ullTotalPhys
            avail_bytes = mem.ullAvailPhys
            mem_load_pct = int(mem.dwMemoryLoad)
    elif hasattr(os, "sysconf"):
        page = os.sysconf("SC_PAGE_SIZE")
        total_bytes = page * os.sysconf("SC_PHYS_PAGES")
        avail_bytes = page * os.sysconf("SC_AVPHYS_PAGES")

    total_gib = round(total_bytes / (1024**3), 2) if total_bytes else 0.0
    avail_gib = round(avail_bytes / (1024**3), 2) if avail_bytes else 0.0
    tier = 1 if total_gib < 6 else 2 if total_gib < 16 else 3

    disks: dict[str, dict] = {}
    if os.name == "nt":
        for drive in ("C:/", "D:/"):
            if Path(drive).exists():
                try:
                    usage = shutil.disk_usage(drive)
                    disks[drive[0] + ":"] = {
                        "total_gib": round(usage.total / (1024**3), 2),
                        "free_gib": round(usage.free / (1024**3), 2),
                        "used_pct": round((usage.used / usage.total) * 100, 1) if usage.total else 0.0,
                    }
                except OSError:
                    pass
    else:
        try:
            usage = shutil.disk_usage(hermes_home if hermes_home.exists() else Path.home())
            disks["root"] = {
                "total_gib": round(usage.total / (1024**3), 2),
                "free_gib": round(usage.free / (1024**3), 2),
                "used_pct": round((usage.used / usage.total) * 100, 1) if usage.total else 0.0,
            }
        except OSError:
            pass

    return {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "python_version": platform.python_version(),
        "cpu_cores": os.cpu_count() or 1,
        "physical_ram_gib": total_gib,
        "available_ram_gib": avail_gib,
        "memory_load_pct": mem_load_pct,
        "ram_tier": tier,
        "win10_4gb_lite_recommended": total_gib <= 6.0 or avail_gib < 1.5,
        "drive_d_available": os.name == "nt" and Path("D:/").exists(),
        "disks": disks,
    }


def inspect_profiles_passive(hermes_home: Path) -> list[dict]:
    """Passively inspect native profiles without reading any secrets or model values."""
    results: list[dict] = []
    if not hermes_home.is_dir():
        return results
    candidates: list[tuple[str, Path]] = [("default", hermes_home)]
    profiles_dir = hermes_home / "profiles"
    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir() or not PROFILE_ID_RE.fullmatch(entry.name):
                continue
            if (profiles_dir / ".deleted" / entry.name).exists():
                continue
            candidates.append((entry.name, entry))
    for pid, folder in candidates:
        soul_health = check_soul_health_passive(folder / "SOUL.md")
        if pid == "default" and soul_health["status"] == "MISSING":
            soul_health["status"] = "OPTIONAL_ABSENT"
        skills_dir = folder / "skills"
        skill_count = sum(1 for _ in skills_dir.glob("*/SKILL.md")) if skills_dir.is_dir() else 0
        trim_installed = (folder / "plugins/msshi-auto-trim/plugin.yaml").is_file()
        interaction_installed = (folder / "plugins/msshi-interaction/plugin.yaml").is_file()
        results.append({
            "profile": pid,
            "config_present": (folder / "config.yaml").is_file(),
            "profile_meta_present": (folder / "profile.yaml").is_file(),
            "env_present": (folder / ".env").is_file(),
            "skill_folders": skill_count,
            "trim_plugin_present": trim_installed,
            "interaction_plugin_present": interaction_installed,
            "soul_health": soul_health,
        })
    return results


def discover_receipts(hermes_home: Path, *, limit: int = 40) -> list[dict]:
    """List rollback receipts across online-repair, profile-receipts, backups, and soul-receipts."""
    receipts: list[dict] = []
    rt = hermes_home / "msshi-runtime"
    if not rt.is_dir():
        return receipts

    roots = [
        ("profile-receipts", rt / "profile-receipts"),
        ("runtime-backup", rt / "backups"),
        ("soul-receipts", rt / "soul-receipts"),
    ]
    seen_ids: set[str] = set()
    for category, base in roots:
        if not base.is_dir():
            continue
        for receipt_path in base.glob("*/receipt.json"):
            if is_zone3_protected(receipt_path):
                continue
            try:
                reject_links(receipt_path)
                raw = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
                if not isinstance(raw, dict):
                    continue
                stat_info = receipt_path.stat()
                kind = str(raw.get("kind") or category)
                files = raw.get("files") or raw.get("entries") or raw.get("changes") or []
                file_count = len(files) if isinstance(files, list) else 0
                receipt_id = str(raw.get("receipt_id") or receipt_path.parent.name)
                if receipt_id in seen_ids:
                    continue
                seen_ids.add(receipt_id)
                timestamp = (
                    raw.get("timestamp_utc")
                    or raw.get("updated_at")
                    or raw.get("created_utc")
                    or datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc).isoformat()
                )
                receipts.append({
                    "receipt_id": receipt_id,
                    "category": category,
                    "kind": kind,
                    "path": str(receipt_path),
                    "timestamp_utc": str(timestamp),
                    "state": str(raw.get("state") or ("applied" if raw.get("changes") else "recorded")),
                    "file_count": file_count,
                    "github_ref": raw.get("github_ref"),
                    "commit_sha": raw.get("commit_sha"),
                    "target_root": raw.get("target_root") or raw.get("home") or raw.get("hermes_home"),
                    "can_one_click_rollback": kind == "msshi-doctor-online-repair" and raw.get("state") in (
                        "applied", "prepared", "aborted_partial", "rollback_conflicts",
                    ),
                    "rollback_kind": (
                        "online_repair"
                        if kind == "msshi-doctor-online-repair"
                        else "runtime_migration"
                        if category == "runtime-backup" and isinstance(raw.get("changes"), list)
                        else "profile_transaction"
                        if category == "profile-receipts"
                        else "soul_protocol"
                    ),
                })
            except (OSError, ValueError):
                continue

    receipts.sort(key=lambda r: r.get("timestamp_utc", ""), reverse=True)
    return receipts[:limit]


def build_five_honest_statuses(
    doctor_data: dict | None,
    runtime_data: dict | None,
    interaction_data: dict | None,
    trim_data: dict | None,
    connect_data: dict | None,
) -> list[dict]:
    """Produce the 5 distinct honest status dimensions (never collapsed into a fake READY)."""
    # 1. Instruksi Ditemukan (SKILL.md parity)
    if doctor_data and doctor_data.get("parity_ok") is True:
        s1 = {
            "id": "instructions",
            "title": "1. Instruksi Ditemukan",
            "status": "OK",
            "badge": "PARITY_OK",
            "detail": (
                f"{doctor_data.get('bundled_skills', 0)} skill bundel cocok "
                f"({doctor_data.get('core_skills', 0)} inti + {doctor_data.get('wrappers', 0)} wrapper)."
            ),
        }
    elif doctor_data:
        s1 = {
            "id": "instructions",
            "title": "1. Instruksi Ditemukan",
            "status": "WARN",
            "badge": "NEEDS_REVIEW",
            "detail": "Paritas folder skill dan katalog perlu diperiksa.",
        }
    else:
        s1 = {
            "id": "instructions",
            "title": "1. Instruksi Ditemukan",
            "status": "UNKNOWN",
            "badge": "CLI_UNAVAILABLE",
            "detail": "Output msshi doctor --json belum tersedia.",
        }

    # 2. Registered routes; startup policy can be absent from older status reports.
    conns = (connect_data or {}).get("connections") or []
    reg_count = sum(1 for c in conns if c.get("registered"))
    if connect_data is not None:
        policy = connect_data.get("auto_open_on_boot")
        boot_detail = {True: "aktif", False: "nonaktif"}.get(policy) if isinstance(policy, bool) else "belum diketahui"
        s2 = {
            "id": "routes",
            "title": "2. Rute Terdaftar",
            "status": "OK" if reg_count > 0 else "WARN",
            "badge": f"{reg_count}_REGISTERED",
            "detail": f"{reg_count} rute terdaftar pada profil aktif. Buka aplikasi saat startup: {boot_detail}.",
        }
    else:
        s2 = {
            "id": "routes",
            "title": "2. Rute Terdaftar",
            "status": "UNKNOWN",
            "badge": "NOT_INSPECTED",
            "detail": "Status msshi connect --status belum terbaca.",
        }

    # 3. Runtime Tersedia (MSSHITrim binary, Python modules, Git/Node, runtime migration)
    trim_ok = bool((trim_data or {}).get("runtime_present") and (trim_data or {}).get("runtime_hash_matches_installer_record"))
    py_mods = (doctor_data or {}).get("python_modules") or {}
    missing_mods = [k for k, v in py_mods.items() if not v]
    rt_ok = bool((runtime_data or {}).get("migrated")) and trim_ok and not missing_mods
    s3 = {
        "id": "runtime",
        "title": "3. Runtime Tersedia",
        "status": "OK" if rt_ok else "WARN",
        "badge": "LOCAL_VERIFIED" if rt_ok else "PARTIAL_RUNTIME",
        "detail": (
            f"MSSHITrim={'terverifikasi' if trim_ok else 'belum siap'}, "
            f"migrasi v3={'aktif' if (runtime_data or {}).get('migrated') else 'belum'}, "
            f"modul dokumen kurang={len(missing_mods)}."
        ),
    }

    # 4. Akun/Izin Siap (User-owned config, Advisor/Council configured vs NEEDS_COUNCIL_CONFIG)
    int_profiles = (interaction_data or {}).get("profiles") or []
    needs_cfg = [
        p.get("role")
        for p in int_profiles
        if p.get("council_readiness") == "NEEDS_COUNCIL_CONFIG"
        or p.get("advisor_readiness") == "NEEDS_ADVISOR_CONFIG"
    ]
    configured_count = sum(
        1
        for p in int_profiles
        if p.get("council_readiness") == "CONFIGURED_NOT_PROVIDER_TESTED"
        or p.get("advisor_readiness") == "CONFIGURED_NOT_PROVIDER_TESTED"
    )
    if int_profiles and not needs_cfg:
        s4 = {
            "id": "accounts_permissions",
            "title": "4. Akun & Konfigurasi",
            "status": "INFO",
            "badge": "CONFIGURED_LOCAL",
            "detail": f"{configured_count}/{len(int_profiles)} peran terkonfigurasi lokal oleh pengguna (provider belum diuji).",
        }
    elif needs_cfg:
        s4 = {
            "id": "accounts_permissions",
            "title": "4. Akun & Konfigurasi",
            "status": "WARN",
            "badge": "NEEDS_USER_CONFIG",
            "detail": f"Perlu pilihan model pengguna di Hermes untuk: {', '.join(needs_cfg)}.",
        }
    else:
        s4 = {
            "id": "accounts_permissions",
            "title": "4. Akun & Konfigurasi",
            "status": "INFO",
            "badge": "USER_MANAGED",
            "detail": "Model, provider, endpoint, dan API key dikelola mandiri oleh pengguna di Hermes.",
        }

    # 5. Belum Diuji (Honest explicit separation of paid/live execution)
    s5 = {
        "id": "live_execution",
        "title": "5. Uji Eksekusi Nyata",
        "status": "INFO",
        "badge": "NOT_TESTED_0_CREDITS",
        "detail": "0 panggilan model/API berbayar, 0 render/transkripsi berbayar dijalankan oleh Doctor.",
    }
    return [s1, s2, s3, s4, s5]


def collect_local_snapshot(
    *,
    hermes_home: str | Path | None = None,
    msshi_home: str | Path | None = None,
    target_root: str | Path | None = None,
    run_cli: bool = True,
) -> dict:
    """Gather complete read-only local diagnostic snapshot without exposing any secrets."""
    h_home = resolve_hermes_home(hermes_home)
    m_home = resolve_msshi_home(msshi_home)
    cli_state = read_cli_state(m_home)
    resolved_root, root_source = resolve_msshi_target_root(
        target_root, msshi_home=m_home, hermes_home=h_home
    )
    target_ref = detect_target_ref(resolved_root)
    hw = collect_hardware_telemetry(h_home)
    passive_profiles = inspect_profiles_passive(h_home)
    receipts = discover_receipts(h_home)

    doctor_res = {"ok": False, "exit_code": None, "data": None}
    runtime_res = {"ok": False, "exit_code": None, "data": None}
    interaction_res = {"ok": False, "exit_code": None, "data": None}
    trim_res = {"ok": False, "exit_code": None, "data": None}
    connect_res = {"ok": False, "exit_code": None, "data": None}

    target_exists = resolved_root is not None and resolved_root.is_dir()
    if run_cli and target_exists:
        homes = {"msshi_home": msshi_home, "hermes_home": h_home}
        doctor_res = _run_msshi_json(["doctor", "--json"], **homes)
        runtime_res = _run_msshi_json(["runtime", "status", "--home", str(h_home), "--json"], **homes)
        interaction_res = _run_msshi_json(["interaction", "--status", "--home", str(h_home)], **homes)
        trim_res = _run_msshi_json(["trim", "--status", "--home", str(h_home), "--profile", "default"], **homes)
        connect_res = _run_msshi_json(["connect", "--status", "--hermes-home", str(h_home), "--json"], **homes)

    doctor_data = doctor_res.get("data") if isinstance(doctor_res.get("data"), dict) else None
    runtime_data = runtime_res.get("data") if isinstance(runtime_res.get("data"), dict) else None
    interaction_data = interaction_res.get("data") if isinstance(interaction_res.get("data"), dict) else None
    trim_data = trim_res.get("data") if isinstance(trim_res.get("data"), dict) else None
    connect_data = connect_res.get("data") if isinstance(connect_res.get("data"), dict) else None

    # Merge passive profile metadata with doctor & interaction statuses
    doc_by_prof = {
        row.get("profile"): row
        for row in ((doctor_data or {}).get("native") or {}).get("profiles", [])
        if isinstance(row, dict)
    }
    int_by_prof = {
        row.get("profile"): row
        for row in (interaction_data or {}).get("profiles", [])
        if isinstance(row, dict)
    }
    merged_profiles = []
    for p in passive_profiles:
        pid = p["profile"]
        drow = doc_by_prof.get(pid, {})
        irow = int_by_prof.get(pid, {})
        merged_profiles.append({
            **p,
            "role": irow.get("role") or drow.get("role") or pid,
            "interaction_type": irow.get("type"),
            "trim_status": drow.get("msshi-auto-trim") or ("INSTALLED" if p["trim_plugin_present"] else "NOT_INSTALLED"),
            "interaction_status": drow.get("msshi-interaction") or ("INSTALLED" if p["interaction_plugin_present"] else "NOT_INSTALLED"),
            "advisor_readiness": irow.get("advisor_readiness") or drow.get("advisor_readiness"),
            "council_readiness": irow.get("council_readiness") or drow.get("council_readiness"),
            "reference_count": irow.get("reference_count", 0),
            "auto_open_on_boot": drow.get("auto_open_on_boot"),
            "provider_execution_tested": False,
        })

    five_statuses = build_five_honest_statuses(
        doctor_data, runtime_data, interaction_data, trim_data, connect_data
    )

    if not target_exists:
        overall_status, exit_code = "CORE_ERROR", EXIT_CORE_ERROR
    elif doctor_data and "status" in doctor_data and "exit_code" in doctor_data:
        overall_status, exit_code = doctor_data["status"], int(doctor_data["exit_code"])
    else:
        overall_status, exit_code = "NEEDS_ATTENTION", EXIT_NEEDS_ATTENTION

    return {
        "schema_version": 1,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "model_calls": 0,
        "secrets_exposed": False,
        "hermes_home": str(h_home),
        "msshi_home": str(m_home),
        "target_root": str(resolved_root) if resolved_root else None,
        "target_root_source": root_source,
        "target_ref": target_ref,
        "cli_state": cli_state,
        "hardware": hw,
        "five_statuses": five_statuses,
        "profiles": merged_profiles,
        "receipts_count": len(receipts),
        "doctor": doctor_data,
        "doctor_exit_code": doctor_res.get("exit_code"),
        "runtime": runtime_data,
        "interaction": interaction_data,
        "trim_default": trim_data,
        "connect_default": connect_data,
        "status": overall_status,
        "exit_code": exit_code,
    }
