"""Ownership-aware 3-category Online GitHub Diff & Offline Fallback Engine."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

from .constants import (
    CORE_ZONE1_DIRS,
    DEFAULT_REF,
    EXIT_CORE_ERROR,
    EXIT_HEALTHY,
    EXIT_NEEDS_ATTENTION,
    FULL_ZONE1_DIRS,
    UPSTREAM_FULL_NAME,
)
from .github_client import GitHubClient, TreeSnapshot
from .safety import (
    SecurityViolationError,
    check_soul_health_passive,
    classify_repo_path,
    detect_corruption,
    git_blob_sha1,
    is_reparse_or_symlink,
    is_zone2_user_owned,
    is_zone3_protected,
    matches_git_blob_sha,
    reject_links,
    safe_join_within_root,
    sha256_bytes,
)


def build_local_tree_snapshot(
    fallback_root: Path, *, ref: str = DEFAULT_REF, scope: str = "core"
) -> TreeSnapshot:
    """Index only the selected scope of a local reference, without opening protected files."""
    if is_zone3_protected(fallback_root):
        raise SecurityViolationError("Protected path cannot be used as a fallback reference.")
    root = reject_links(fallback_root)
    entries: dict[str, dict] = {}
    allowed_dirs = FULL_ZONE1_DIRS if scope == "full" else CORE_ZONE1_DIRS
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            dirnames[:] = [
                d
                for d in dirnames
                if (current != root or d in allowed_dirs)
                and d.casefold() not in {".git", "__pycache__", "node_modules", ".venv", "venv"}
                and not is_zone2_user_owned(d)
                and not is_zone3_protected(current / d)
                and not is_reparse_or_symlink(current / d)
            ]
            for name in filenames:
                fpath = current / name
                rel = fpath.relative_to(root).as_posix()
                if classify_repo_path(rel, scope=scope) != "zone1_core":
                    continue
                if is_reparse_or_symlink(fpath) or not fpath.is_file():
                    continue
                try:
                    raw = fpath.read_bytes()
                except OSError:
                    continue
                entries[rel] = {"sha": git_blob_sha1(raw), "size": len(raw)}
    return TreeSnapshot(
        status="OFFLINE_LOCAL_FALLBACK",
        repo=UPSTREAM_FULL_NAME,
        ref=ref,
        commit_sha=None,
        entries=entries,
        from_cache=False,
        fallback_available=bool(entries),
    )


def scan_zone2_user_files(hermes_home: Path | None) -> list[dict]:
    """Passively summarize Zone 2 user-owned files without reading or leaking secret values."""
    items: list[dict] = []
    if hermes_home is None or is_zone3_protected(hermes_home):
        return items
    hermes_home = reject_links(hermes_home)
    if not hermes_home.is_dir():
        return items
    profile_dirs: list[tuple[str, Path]] = [("default", hermes_home)]
    p_root = hermes_home / "profiles"
    if not is_reparse_or_symlink(p_root) and p_root.is_dir():
        for entry in sorted(p_root.iterdir()):
            if entry.name.startswith(".") or is_zone3_protected(entry) or is_reparse_or_symlink(entry):
                continue
            if entry.is_dir() and not (p_root / ".deleted" / entry.name).exists():
                profile_dirs.append((entry.name, entry))

    for pid, folder in profile_dirs:
        if is_zone3_protected(folder):
            continue
        soul_path = folder / "SOUL.md"
        soul_info = check_soul_health_passive(soul_path)
        soul_status = soul_info["status"]
        if pid == "default" and soul_status == "MISSING":
            soul_status = "OPTIONAL_ABSENT"
        items.append({
            "zone": "ZONE_2_USER_OWNED",
            "profile": pid,
            "name": "SOUL.md",
            "path": str(soul_path),
            "status": soul_status,
            "exists": soul_info["exists"],
            "valid_utf8": soul_info["valid_utf8"],
            "nul_free": soul_info["nul_free"],
            "auto_repair_allowed": False,
            "note": (
                "SOUL.md sehat dan dilindungi (tidak pernah ditimpa otomatis)."
                if soul_status == "HEALTHY"
                else "SOUL.md belum dibuat pada profil default (opsional)."
                if soul_status == "OPTIONAL_ABSENT"
                else "SOUL.md perlu tinjauan/pemulihan manual dari receipt; tidak ditimpa otomatis."
            ),
        })
        for fname in ("config.yaml", ".env", "auth.json"):
            fpath = folder / fname
            if not is_reparse_or_symlink(fpath) and fpath.is_file():
                items.append({
                    "zone": "ZONE_2_USER_OWNED",
                    "profile": pid,
                    "name": fname,
                    "path": str(fpath),
                    "status": "PROTECTED_PRESENT",
                    "exists": True,
                    "auto_repair_allowed": False,
                    "note": "File konfigurasi/kredensial pengguna — nilai tidak dibaca dan tidak pernah ditimpa.",
                })
    return items


def compare_against_tree(
    target_root: Path,
    snapshot: TreeSnapshot,
    *,
    hermes_home: Path | None = None,
    scope: str = "core",
) -> dict:
    """Compare local MSSHI root against an upstream Git tree under strict 3-zone rules."""
    clean_root = reject_links(target_root)
    zone1_files: list[dict] = []
    counts = {
        "total_zone1": 0,
        "healthy": 0,
        "missing": 0,
        "corrupted": 0,
        "drifted": 0,
        "excluded_non_core": 0,
    }
    repairable_download_bytes = 0

    for rel_path in sorted(snapshot.entries):
        zone = classify_repo_path(rel_path, scope=scope)
        if zone != "zone1_core":
            counts["excluded_non_core"] += 1
            continue

        meta = snapshot.entries[rel_path]
        expected_blob_sha = str(meta["sha"])
        upstream_size = int(meta.get("size") or 0)
        counts["total_zone1"] += 1

        try:
            local_path = safe_join_within_root(clean_root, rel_path)
        except SecurityViolationError as exc:
            counts["corrupted"] += 1
            zone1_files.append({
                "rel_path": rel_path,
                "zone": "ZONE_1_MSSHI_CORE",
                "status": "CORRUPTED",
                "reason": "unsafe_path_or_link",
                "detail": str(exc),
                "upstream_blob_sha": expected_blob_sha,
                "upstream_size": upstream_size,
                "local_size": 0,
                "local_sha256": None,
                "repairable": False,
            })
            continue

        if not local_path.exists():
            counts["missing"] += 1
            repairable_download_bytes += upstream_size
            zone1_files.append({
                "rel_path": rel_path,
                "zone": "ZONE_1_MSSHI_CORE",
                "status": "MISSING",
                "reason": "file_missing",
                "upstream_blob_sha": expected_blob_sha,
                "upstream_size": upstream_size,
                "local_size": 0,
                "local_sha256": None,
                "repairable": True,
            })
            continue

        if not local_path.is_file():
            counts["corrupted"] += 1
            zone1_files.append({
                "rel_path": rel_path,
                "zone": "ZONE_1_MSSHI_CORE",
                "status": "CORRUPTED",
                "reason": "not_regular_file",
                "upstream_blob_sha": expected_blob_sha,
                "upstream_size": upstream_size,
                "local_size": 0,
                "local_sha256": None,
                "repairable": False,
            })
            continue

        try:
            local_bytes = local_path.read_bytes()
        except OSError:
            counts["corrupted"] += 1
            zone1_files.append({
                "rel_path": rel_path,
                "zone": "ZONE_1_MSSHI_CORE",
                "status": "CORRUPTED",
                "reason": "unreadable_file",
                "upstream_blob_sha": expected_blob_sha,
                "upstream_size": upstream_size,
                "local_size": 0,
                "local_sha256": None,
                "repairable": False,
            })
            continue

        local_sha256 = sha256_bytes(local_bytes)
        corruption_reason = detect_corruption(rel_path, local_bytes, expected_size=upstream_size)
        if corruption_reason is not None:
            counts["corrupted"] += 1
            repairable_download_bytes += upstream_size
            zone1_files.append({
                "rel_path": rel_path,
                "zone": "ZONE_1_MSSHI_CORE",
                "status": "CORRUPTED",
                "reason": corruption_reason,
                "upstream_blob_sha": expected_blob_sha,
                "upstream_size": upstream_size,
                "local_size": len(local_bytes),
                "local_sha256": local_sha256,
                "repairable": True,
            })
            continue

        if matches_git_blob_sha(rel_path, local_bytes, expected_blob_sha):
            counts["healthy"] += 1
            zone1_files.append({
                "rel_path": rel_path,
                "zone": "ZONE_1_MSSHI_CORE",
                "status": "HEALTHY",
                "reason": "exact_match",
                "upstream_blob_sha": expected_blob_sha,
                "upstream_size": upstream_size,
                "local_size": len(local_bytes),
                "local_sha256": local_sha256,
                "repairable": False,
            })
        else:
            counts["drifted"] += 1
            repairable_download_bytes += upstream_size
            zone1_files.append({
                "rel_path": rel_path,
                "zone": "ZONE_1_MSSHI_CORE",
                "status": "DRIFTED",
                "reason": "hash_mismatch",
                "upstream_blob_sha": expected_blob_sha,
                "upstream_size": upstream_size,
                "local_size": len(local_bytes),
                "local_sha256": local_sha256,
                "repairable": True,
            })

    zone2_items = scan_zone2_user_files(hermes_home)
    zone2_soul_issues = [
        item
        for item in zone2_items
        if item["name"] == "SOUL.md" and item["status"] not in ("HEALTHY", "OPTIONAL_ABSENT")
    ]

    # Strict Zone 3 policy summary (never scanned/opened)
    zone3_policy = [
        "1. Private (dilarang total untuk semua AI/scanner)",
        "2. Data Diri atau Perkembangan Diri (NP) (khusus tulis manusia)",
        "HermesData/data/bin/ (nlm.exe, notebooklm-mcp.exe)",
        "Konfigurasi rclone (gdrive:)",
        "MSSHI_Recovery/",
    ]

    trusted_online_tree = bool(snapshot.commit_sha) and snapshot.status in ("ONLINE_OK", "ONLINE_ETAG_CACHED")
    if not trusted_online_tree:
        for row in zone1_files:
            row["repairable"] = False
        repairable_download_bytes = 0
    if counts["corrupted"] > 0:
        overall = "CORRUPTED_FILES_DETECTED"
        exit_code = EXIT_CORE_ERROR
    elif not trusted_online_tree:
        overall = snapshot.status
        exit_code = EXIT_NEEDS_ATTENTION
    elif not counts["total_zone1"] or counts["missing"] > 0 or counts["drifted"] > 0 or zone2_soul_issues:
        overall = "NEEDS_ATTENTION"
        exit_code = EXIT_NEEDS_ATTENTION
    else:
        overall = "HEALTHY"
        exit_code = EXIT_HEALTHY

    return {
        "schema_version": 1,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "model_calls": 0,
        "secrets_exposed": False,
        "connection_status": snapshot.status,
        "from_cache": snapshot.from_cache,
        "etag": snapshot.etag,
        "rate_limit_remaining": snapshot.rate_limit_remaining,
        "rate_limit_reset_utc": snapshot.rate_limit_reset_utc,
        "error_message": snapshot.error_message,
        "fallback_available": snapshot.fallback_available,
        "github_repo": snapshot.repo,
        "github_ref": snapshot.ref,
        "commit_sha": snapshot.commit_sha,
        "scope": scope,
        "target_root": str(clean_root),
        "summary": {
            **counts,
            "repairable_count": sum(row["repairable"] for row in zone1_files),
            "repairable_download_bytes": repairable_download_bytes,
            "zone2_user_protected_count": len(zone2_items),
            "zone2_soul_warnings": len(zone2_soul_issues),
            "zone3_strict_rules_count": len(zone3_policy),
        },
        "files": zone1_files,
        "zone2_user_files": zone2_items,
        "zone3_protected_policy": zone3_policy,
        "status": overall,
        "exit_code": exit_code,
    }


def run_online_diff(
    target_root: Path,
    *,
    ref: str = DEFAULT_REF,
    hermes_home: Path | None = None,
    scope: str = "core",
    client: GitHubClient | None = None,
    cache_dir: Path | None = None,
    local_fallback_root: Path | None = None,
) -> dict:
    """Execute read-only online GitHub diff with ETag caching and honest offline fallback."""
    gh = client or GitHubClient(cache_dir=cache_dir)
    snapshot = gh.fetch_tree(ref)

    used_local_fallback = False
    if snapshot.status in ("OFFLINE", "RATE_LIMITED", "ERROR") and not snapshot.entries:
        if (local_fallback_root and not is_zone3_protected(local_fallback_root)
                and reject_links(local_fallback_root) != reject_links(target_root)):
            fb = build_local_tree_snapshot(local_fallback_root, ref=ref, scope=scope)
        else:
            fb = None
        if fb is not None and fb.fallback_available:
            fb.error_message = snapshot.error_message
            fb.rate_limit_remaining = snapshot.rate_limit_remaining
            fb.rate_limit_reset_utc = snapshot.rate_limit_reset_utc
            # Keep honest network status tag so UI shows OFFLINE / RATE_LIMITED banner clearly
            fb.status = f"{snapshot.status}_WITH_LOCAL_FALLBACK"
            snapshot = fb
            used_local_fallback = True

    report = compare_against_tree(target_root, snapshot, hermes_home=hermes_home, scope=scope)
    report["used_local_fallback"] = used_local_fallback
    return report
