"""6-Step Atomic Online Safe Repair Engine with Backup, Verification, Receipt & 1-Click Rollback."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import uuid

from .constants import DEFAULT_REF, EXIT_CORE_ERROR, EXIT_HEALTHY, EXIT_NEEDS_ATTENTION
from .diff_engine import run_online_diff
from .github_client import GitHubClient
from .local_probe import _hidden_subprocess_kwargs, _run_msshi_json, find_msshi_executable
from .safety import (
    SecurityViolationError,
    atomic_write_bytes,
    atomic_write_json,
    classify_repo_path,
    git_blob_sha1,
    is_zone2_user_owned,
    is_zone3_protected,
    normalize_rel_path,
    reject_links,
    safe_join_within_root,
    sha256_bytes,
    validate_commit_sha,
)


def _select_repair_candidates(diff_report: dict, selected_files: list[str] | None = None) -> list[dict]:
    """Filter Zone 1 repairable files strictly; reject any Zone 2 / Zone 3 / traversal path."""
    by_path = {row["rel_path"]: row for row in diff_report.get("files", [])}
    scope = diff_report.get("scope", "core")
    if selected_files is not None:
        normalized_selection = []
        for raw in selected_files:
            clean = normalize_rel_path(raw)
            if is_zone3_protected(clean):
                raise SecurityViolationError(f"Zone 3 strictly protected path cannot be repaired: {clean}")
            if is_zone2_user_owned(clean):
                raise SecurityViolationError(f"Zone 2 user-owned file cannot be auto-repaired: {clean}")
            if classify_repo_path(clean, scope=scope) != "zone1_core":
                raise SecurityViolationError(f"Path outside MSSHI Zone 1 core scope: {clean}")
            if clean not in by_path:
                raise ValueError(f"File not found in upstream GitHub tree: {clean}")
            normalized_selection.append(clean)
        rows = [by_path[p] for p in dict.fromkeys(normalized_selection)]
    else:
        rows = [
            row
            for row in diff_report.get("files", [])
            if row.get("status") in ("MISSING", "CORRUPTED", "DRIFTED") and row.get("repairable")
        ]

    candidates = []
    for row in rows:
        rel = normalize_rel_path(row["rel_path"])
        if is_zone2_user_owned(rel) or is_zone3_protected(rel):
            raise SecurityViolationError(f"Protected file blocked from automatic repair: {rel}")
        if row.get("status") == "HEALTHY":
            continue
        if not row.get("repairable"):
            continue
        candidates.append(row)
    return candidates


def plan_online_repair(
    target_root: Path,
    hermes_home: Path,
    *,
    ref: str = DEFAULT_REF,
    scope: str = "core",
    selected_files: list[str] | None = None,
    client: GitHubClient | None = None,
    cache_dir: Path | None = None,
) -> dict:
    """Step 1 (Dry-Run & Preview): Produce a transparent zero-write repair plan."""
    clean_root = reject_links(target_root)
    clean_home = reject_links(hermes_home)
    diff = run_online_diff(
        clean_root,
        ref=ref,
        hermes_home=clean_home,
        scope=scope,
        client=client,
        cache_dir=cache_dir,
    )
    candidates = _select_repair_candidates(diff, selected_files)
    ts_preview = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_preview = clean_home / "msshi-runtime" / "backups" / f"online-repair-{ts_preview}"
    total_bytes = sum(int(row.get("upstream_size") or 0) for row in candidates)

    can_apply = bool(candidates) and bool(diff.get("commit_sha")) and diff.get("connection_status") in (
        "ONLINE_OK",
        "ONLINE_ETAG_CACHED",
    )

    return {
        "schema_version": 1,
        "operation": "online_repair_preview",
        "dry_run": True,
        "applied": False,
        "can_apply": can_apply,
        "connection_status": diff.get("connection_status"),
        "error_message": diff.get("error_message"),
        "github_repo": diff.get("github_repo"),
        "github_ref": diff.get("github_ref"),
        "commit_sha": diff.get("commit_sha"),
        "scope": scope,
        "target_root": str(clean_root),
        "hermes_home": str(clean_home),
        "backup_dir_preview": str(backup_preview),
        "planned_count": len(candidates),
        "total_download_bytes": total_bytes,
        "planned_files": [
            {
                "rel_path": row["rel_path"],
                "status": row["status"],
                "reason": row.get("reason"),
                "upstream_size": row.get("upstream_size", 0),
                "upstream_blob_sha": row["upstream_blob_sha"],
                "local_sha256": row.get("local_sha256"),
            }
            for row in candidates
        ],
        "zone2_protected_guarantee": "SOUL.md, config.yaml, .env, auth.json, model/provider & custom skills tidak akan disentuh.",
        "zone3_protected_guarantee": "Private, NP, nlm.exe/notebooklm-mcp.exe, rclone & MSSHI_Recovery dilewati total.",
        "model_calls": 0,
        "exit_code": EXIT_HEALTHY if not candidates else EXIT_NEEDS_ATTENTION,
    }


def apply_online_repair(
    target_root: Path,
    hermes_home: Path,
    *,
    ref: str = DEFAULT_REF,
    scope: str = "core",
    selected_files: list[str] | None = None,
    client: GitHubClient | None = None,
    cache_dir: Path | None = None,
    run_recheck: bool = True,
    msshi_home: Path | None = None,
) -> dict:
    """Execute the 6-step atomic online repair: Preview -> Backup -> Download -> Verify -> Receipt -> Re-check."""
    clean_root = reject_links(target_root)
    clean_home = reject_links(hermes_home)
    gh = client or GitHubClient(cache_dir=cache_dir)

    plan = plan_online_repair(
        clean_root,
        clean_home,
        ref=ref,
        scope=scope,
        selected_files=selected_files,
        client=gh,
        cache_dir=cache_dir,
    )
    if plan["planned_count"] == 0:
        return {
            **plan,
            "operation": "online_repair_apply",
            "dry_run": False,
            "applied": False,
            "state": "no_changes_needed",
            "receipt_id": None,
            "receipt_path": None,
            "exit_code": EXIT_HEALTHY,
        }
    if not plan["can_apply"] or not plan.get("commit_sha"):
        return {
            **plan,
            "operation": "online_repair_apply",
            "dry_run": False,
            "applied": False,
            "state": "blocked_offline_or_unpinned",
            "receipt_id": None,
            "receipt_path": None,
            "exit_code": EXIT_CORE_ERROR,
        }

    commit_sha = validate_commit_sha(plan["commit_sha"])
    now = datetime.now(timezone.utc)
    ts_slug = now.strftime("%Y%m%d-%H%M%S")
    rand_id = uuid.uuid4().hex[:8]
    receipt_id = f"online-repair-{ts_slug}-{rand_id}"

    # Step 2: Prepare atomic backup directory & receipt directory
    backup_dir = reject_links(clean_home / "msshi-runtime" / "backups" / f"online-repair-{ts_slug}-{rand_id}")
    receipt_dir = reject_links(clean_home / "msshi-runtime" / "profile-receipts" / receipt_id)
    receipt_path = receipt_dir / "receipt.json"

    # Step 3 & 4 (Pre-flight): Download & cryptographically verify ALL selected files in memory BEFORE touching disk!
    # Also validate every target destination path strictly inside target_root.
    staged: list[dict] = []
    for idx, item in enumerate(plan["planned_files"]):
        rel_path = normalize_rel_path(item["rel_path"])
        dest_path = safe_join_within_root(clean_root, rel_path)
        if is_zone2_user_owned(dest_path) or is_zone3_protected(dest_path):
            raise SecurityViolationError(f"Protected path blocked before write: {dest_path}")
        expected_blob_sha = item["upstream_blob_sha"]
        # Selective download pinned strictly to commit_sha + Git blob SHA-1 check
        downloaded = gh.download_pinned_file(commit_sha, rel_path, expected_blob_sha)
        actual_blob_sha = git_blob_sha1(downloaded)
        if actual_blob_sha != expected_blob_sha:
            raise ValueError(f"Integrity verification failed for {rel_path}")
        before_bytes = dest_path.read_bytes() if dest_path.is_file() else None
        staged.append({
            "index": idx,
            "rel_path": rel_path,
            "target_path": dest_path,
            "status_before": item["status"],
            "before_bytes": before_bytes,
            "sha256_before": sha256_bytes(before_bytes) if before_bytes is not None else None,
            "after_bytes": downloaded,
            "sha256_after": sha256_bytes(downloaded),
            "git_blob_sha_expected": expected_blob_sha,
            "git_blob_sha_actual": actual_blob_sha,
            "backup_file": f"{idx:04d}.bak" if before_bytes is not None else None,
            "size_bytes": len(downloaded),
        })

    # Write backups atomically BEFORE mutating any target file
    backup_dir.mkdir(parents=True, exist_ok=True)
    for row in staged:
        if row["backup_file"] is not None and row["before_bytes"] is not None:
            atomic_write_bytes(backup_dir / row["backup_file"], row["before_bytes"])

    # Step 4 (Commit) with automatic rollback on any write failure
    written_rows: list[dict] = []
    try:
        for row in staged:
            dest: Path = row["target_path"]
            # Guard against concurrent external edits between scan & write
            current_sha = sha256_bytes(dest.read_bytes()) if dest.is_file() else None
            if current_sha != row["sha256_before"]:
                raise ValueError(f"Concurrent modification detected before write: {row['rel_path']}")
            atomic_write_bytes(dest, row["after_bytes"])
            written_rows.append(row)
    except Exception as exc:
        # Automatic transactional rollback of all files written so far
        for row in reversed(written_rows):
            dest = row["target_path"]
            try:
                if row["before_bytes"] is not None:
                    atomic_write_bytes(dest, row["before_bytes"])
                elif dest.exists():
                    dest.unlink()
            except OSError:
                pass
        aborted_receipt = {
            "schema_version": 1,
            "kind": "msshi-doctor-online-repair",
            "receipt_id": receipt_id,
            "timestamp_utc": now.isoformat(),
            "github_repo": plan["github_repo"],
            "github_ref": plan["github_ref"],
            "commit_sha": commit_sha,
            "target_root": str(clean_root),
            "hermes_home": str(clean_home),
            "backup_dir": str(backup_dir),
            "state": "aborted_rolled_back",
            "error": str(exc),
            "files": [],
        }
        receipt_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(receipt_path, aborted_receipt)
        raise

    # Step 5: Persist complete Receipt JSON
    receipt_payload = {
        "schema_version": 1,
        "kind": "msshi-doctor-online-repair",
        "receipt_id": receipt_id,
        "timestamp_utc": now.isoformat(),
        "github_repo": plan["github_repo"],
        "github_ref": plan["github_ref"],
        "commit_sha": commit_sha,
        "scope": scope,
        "target_root": str(clean_root),
        "hermes_home": str(clean_home),
        "backup_dir": str(backup_dir),
        "state": "applied",
        "zone2_untouched": True,
        "zone3_untouched": True,
        "model_calls": 0,
        "files": [
            {
                "rel_path": r["rel_path"],
                "target_path": str(r["target_path"]),
                "status_before": r["status_before"],
                "sha256_before": r["sha256_before"],
                "sha256_after": r["sha256_after"],
                "git_blob_sha_expected": r["git_blob_sha_expected"],
                "git_blob_sha_actual": r["git_blob_sha_actual"],
                "backup_file": r["backup_file"],
                "size_bytes": r["size_bytes"],
            }
            for r in staged
        ],
    }
    receipt_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(receipt_path, receipt_payload)
    atomic_write_json(backup_dir / "receipt.json", receipt_payload)

    # Step 6: Automatic Re-check via msshi doctor --json (read-only)
    recheck_report = None
    if run_recheck:
        recheck = _run_msshi_json(["doctor", "--json"], msshi_home=msshi_home)
        recheck_report = recheck.get("data") if recheck.get("ok") else {"error": recheck.get("error")}

    return {
        "schema_version": 1,
        "operation": "online_repair_apply",
        "dry_run": False,
        "applied": True,
        "state": "applied",
        "receipt_id": receipt_id,
        "receipt_path": str(receipt_path),
        "backup_dir": str(backup_dir),
        "github_repo": plan["github_repo"],
        "github_ref": plan["github_ref"],
        "commit_sha": commit_sha,
        "files_repaired": len(staged),
        "files": receipt_payload["files"],
        "zone2_untouched": True,
        "zone3_untouched": True,
        "model_calls": 0,
        "post_repair_doctor": recheck_report,
        "exit_code": EXIT_HEALTHY,
    }


def _find_receipt_path(hermes_home: Path, receipt_id_or_path: str | Path) -> Path:
    raw = Path(receipt_id_or_path)
    if raw.is_file():
        return reject_links(raw)
    clean_home = reject_links(hermes_home)
    candidate = clean_home / "msshi-runtime" / "profile-receipts" / str(receipt_id_or_path) / "receipt.json"
    if candidate.is_file():
        return reject_links(candidate)
    # Fallback search in backups
    backup_cand = clean_home / "msshi-runtime" / "backups" / str(receipt_id_or_path) / "receipt.json"
    if backup_cand.is_file():
        return reject_links(backup_cand)
    raise FileNotFoundError(f"Receipt tidak ditemukan: {receipt_id_or_path}")


def rollback_online_repair(
    hermes_home: Path,
    receipt_id_or_path: str | Path,
    *,
    apply: bool = False,
) -> dict:
    """1-Click Rollback for an online-repair receipt with strict SHA-256 & conflict guards."""
    clean_home = reject_links(hermes_home)
    receipt_path = _find_receipt_path(clean_home, receipt_id_or_path)
    allowed_roots = (
        reject_links(clean_home / "msshi-runtime" / "profile-receipts"),
        reject_links(clean_home / "msshi-runtime" / "backups"),
    )
    if not any(receipt_path.is_relative_to(r) for r in allowed_roots):
        raise SecurityViolationError("Receipt berada di luar ruang msshi-runtime Hermes home.")

    payload = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    if payload.get("kind") != "msshi-doctor-online-repair":
        raise ValueError("Receipt ini bukan tipe msshi-doctor-online-repair.")
    if Path(payload.get("hermes_home", "")).absolute() != clean_home.absolute():
        raise SecurityViolationError("Receipt milik instalasi Hermes lain.")

    target_root = reject_links(Path(payload["target_root"]))
    backup_dir = reject_links(Path(payload["backup_dir"]))
    if not backup_dir.is_relative_to(clean_home / "msshi-runtime" / "backups"):
        raise SecurityViolationError("Lokasi backup berada di luar msshi-runtime/backups.")

    restored: list[str] = []
    removed_new_files: list[str] = []
    conflicts: list[dict] = []

    for row in reversed(payload.get("files", [])):
        rel = normalize_rel_path(row["rel_path"])
        dest = safe_join_within_root(target_root, rel)
        expected_after = row.get("sha256_after")
        current_sha = sha256_bytes(dest.read_bytes()) if dest.is_file() else None
        if current_sha != expected_after:
            conflicts.append({
                "rel_path": rel,
                "reason": "modified_after_repair",
                "expected_sha256": expected_after,
                "current_sha256": current_sha,
            })
            continue

        backup_name = row.get("backup_file")
        if backup_name:
            backup_path = safe_join_within_root(backup_dir, backup_name)
            if not backup_path.is_file():
                conflicts.append({"rel_path": rel, "reason": "backup_file_missing"})
                continue
            before_bytes = backup_path.read_bytes()
            if sha256_bytes(before_bytes) != row.get("sha256_before"):
                raise ValueError(f"Checksum backup rusak untuk {rel}; rollback dibatalkan.")
            if apply:
                atomic_write_bytes(dest, before_bytes)
            restored.append(rel)
        else:
            # File did not exist before repair (was MISSING) -> remove cleanly on rollback
            if apply and dest.is_file():
                dest.unlink()
            removed_new_files.append(rel)

    new_state = (
        ("rollback_conflicts" if conflicts else "rolled_back")
        if apply
        else "rollback_preview"
    )
    result = {
        "schema_version": 1,
        "operation": "online_repair_rollback",
        "dry_run": not apply,
        "applied": apply,
        "receipt_id": payload.get("receipt_id"),
        "receipt_path": str(receipt_path),
        "state": new_state,
        "restored_files": restored,
        "removed_created_files": removed_new_files,
        "conflicts": conflicts,
        "exit_code": EXIT_NEEDS_ATTENTION if conflicts else EXIT_HEALTHY,
    }
    if apply:
        payload["state"] = new_state
        payload["rollback"] = {
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "restored_files": restored,
            "removed_created_files": removed_new_files,
            "conflicts": conflicts,
        }
        atomic_write_json(receipt_path, payload)
    return result


def run_local_doctor_fix(*, msshi_home: Path | None = None) -> dict:
    """Invoke native `msshi doctor --fix --json` explicitly when chosen by user."""
    exe = find_msshi_executable(msshi_home)
    if not exe:
        return {"ok": False, "error": "msshi CLI tidak ditemukan.", "exit_code": EXIT_CORE_ERROR}
    try:
        proc = subprocess.run(
            [*exe, "doctor", "--fix", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **_hidden_subprocess_kwargs(),
        )
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return {"ok": True, "exit_code": proc.returncode, "data": data}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc), "exit_code": EXIT_CORE_ERROR}
