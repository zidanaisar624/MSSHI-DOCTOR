"""CLI-first entrypoint for MSSHI Doctor App (automation-ready JSON & human output)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .constants import (
    APP_NAME,
    APP_VERSION,
    EXIT_CORE_ERROR,
    EXIT_HEALTHY,
    EXIT_NEEDS_ATTENTION,
    UPSTREAM_FULL_NAME,
    resolve_cache_dir,
)
from .diff_engine import run_online_diff
from .github_client import GitHubClient
from .local_probe import (
    collect_local_snapshot,
    detect_target_ref,
    discover_receipts,
    resolve_hermes_home,
    resolve_msshi_home,
    resolve_msshi_target_root,
)
from .repair_engine import (
    apply_online_repair,
    plan_online_repair,
    rollback_online_repair,
    run_local_doctor_fix,
)
from .self_update import check_app_update, run_self_update
from .troubleshooter import run_all_troubleshooter_checks


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except OSError:
                pass


def _parse_files_arg(raw_files: list[str] | None) -> list[str] | None:
    if not raw_files:
        return None
    parsed: list[str] = []
    for token in raw_files:
        for piece in token.split(","):
            cleaned = piece.strip()
            if cleaned:
                parsed.append(cleaned)
    return parsed or None


def _emit_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_local_human(report: dict) -> None:
    hw = report.get("hardware", {})
    print(f"{APP_NAME} v{APP_VERSION} — Diagnostik Lokal (Read-Only, 0 Model Calls)")
    print(f"Status Umum   : {report.get('status')} (exit_code={report.get('exit_code')})")
    print(f"Hermes Home   : {report.get('hermes_home')}")
    print(f"MSSHI Home    : {report.get('msshi_home')}")
    print(f"Target Root   : {report.get('target_root') or 'TIDAK DITEMUKAN'} ({report.get('target_root_source')})")
    print(f"Target Ref    : {report.get('target_ref')}")
    print(
        f"Perangkat     : {hw.get('os')} | CPU {hw.get('cpu_cores')} core | "
        f"RAM {hw.get('available_ram_gib')}/{hw.get('physical_ram_gib')} GiB (Tier {hw.get('ram_tier')})"
    )
    print("\n[5 Status Jujur]")
    for item in report.get("five_statuses", []):
        print(f"  - {item['title']}: [{item['badge']}] {item['detail']}")
    profiles = report.get("profiles", [])
    print(f"\n[Profil Native Terdeteksi: {len(profiles)}]")
    for prof in profiles:
        soul = prof.get("soul_health", {}).get("status", "UNKNOWN")
        print(
            f"  - {prof['profile']:10s} | skills={prof.get('skill_folders', 0):3d} "
            f"| SOUL={soul:10s} | trim={prof.get('trim_status')} "
            f"| interaction={prof.get('interaction_type') or '-'}"
        )


def _print_online_human(report: dict) -> None:
    s = report.get("summary", {})
    print(f"{APP_NAME} v{APP_VERSION} — Online GitHub Diff (Zero Full-Clone, Read-Only)")
    print(f"Repo Upstream : {report.get('github_repo')} @ {report.get('github_ref')}")
    print(f"Commit SHA    : {report.get('commit_sha') or '-'}")
    print(
        f"Koneksi       : {report.get('connection_status')} "
        f"(cache={report.get('from_cache')}, rate_limit_remaining={report.get('rate_limit_remaining')})"
    )
    if report.get("error_message"):
        print(f"Catatan       : {report.get('error_message')}")
    print(f"Target Lokal  : {report.get('target_root')} (scope={report.get('scope')})")
    print(
        f"Zona 1 (Core) : total={s.get('total_zone1', 0)}, healthy={s.get('healthy', 0)}, "
        f"missing={s.get('missing', 0)}, corrupted={s.get('corrupted', 0)}, drifted={s.get('drifted', 0)}"
    )
    print(
        f"Zona 2 (User) : {s.get('zone2_user_protected_count', 0)} file dilindungi "
        f"(peringatan SOUL={s.get('zone2_soul_warnings', 0)}, tidak pernah ditimpa otomatis)"
    )
    print(f"Zona 3 (Skip) : {s.get('zone3_strict_rules_count', 0)} jalur lokal sensitif dilewati total")
    issues = [f for f in report.get("files", []) if f.get("status") != "HEALTHY"]
    if issues:
        print(f"\n[File Zona 1 yang Perlu Perhatian ({len(issues)})]")
        for row in issues[:30]:
            print(f"  - [{row['status']:9s}] {row['rel_path']} ({row.get('reason')}, {row.get('upstream_size', 0)} B)")
        if len(issues) > 30:
            print(f"  ... dan {len(issues) - 30} file lainnya (gunakan --json untuk daftar penuh).")
    elif report.get("connection_status") in ("ONLINE_OK", "ONLINE_ETAG_CACHED") and report.get("summary", {}).get("healthy", 0):
        print("\nSemua file inti MSSHI Zona 1 identik dengan rilis GitHub target.")
    else:
        print("\nKecocokan dengan rilis GitHub belum terverifikasi. Periksa koneksi dan ref, lalu ulangi pemeriksaan.")


def _print_repair_human(report: dict) -> None:
    op = report.get("operation")
    print(f"{APP_NAME} v{APP_VERSION} — {op}")
    print(f"Repo / Ref    : {report.get('github_repo')} @ {report.get('github_ref')}")
    print(f"Commit SHA    : {report.get('commit_sha') or '-'}")
    if report.get("verified_upstream") is False:
        print("Rilis GitHub belum terverifikasi. Perbaikan memerlukan koneksi ke GitHub dan commit yang valid.")
    if report.get("dry_run"):
        print(f"Mode          : DRY-RUN PREVIEW (0 file diubah)")
        print(f"Backup Target : {report.get('backup_dir_preview')}")
        print(
            f"Rencana File  : {report.get('planned_count', 0)} file "
            f"({report.get('total_download_bytes', 0)} bytes)"
        )
        for item in report.get("planned_files", [])[:30]:
            print(f"  - [{item['status']:9s}] {item['rel_path']} ({item.get('upstream_size', 0)} B)")
        if report.get("can_apply"):
            print("\nTambahkan --apply untuk mengeksekusi perbaikan aman dengan backup & receipt.")
    else:
        print(f"Status        : {report.get('state')}")
        print(f"Receipt ID    : {report.get('receipt_id') or '-'}")
        print(f"Backup Folder : {report.get('backup_dir') or '-'}")
        print(f"Diperbaiki    : {report.get('files_repaired', 0)} file (Zona 2 & Zona 3 tidak tersentuh)")
        if report.get("post_repair_exit_code") not in (None, EXIT_HEALTHY):
            print("Pemeriksaan lokal setelah perbaikan masih memerlukan perhatian. Jalankan --local untuk rinciannya.")


def _print_receipts_human(receipts: list[dict], hermes_home: Path) -> None:
    print(f"{APP_NAME} v{APP_VERSION} — Daftar Receipt & Backup ({hermes_home})")
    if not receipts:
        print("Belum ada receipt yang tercatat di msshi-runtime.")
        return
    for r in receipts:
        rb = "1-CLICK ROLLBACK" if r.get("can_one_click_rollback") else r.get("rollback_kind")
        print(
            f"  - {r['receipt_id']} | {r['category']} | state={r['state']} "
            f"| files={r['file_count']} | {rb}"
        )


def _print_rollback_human(report: dict) -> None:
    mode = "DRY-RUN PREVIEW" if report.get("dry_run") else "DITERAPKAN"
    print(f"{APP_NAME} v{APP_VERSION} — Rollback Receipt ({mode})")
    print(f"Receipt ID    : {report.get('receipt_id')}")
    print(f"Status        : {report.get('state')}")
    print(f"Dipulihkan    : {len(report.get('restored_files', []))} file")
    print(f"File Baru Hps : {len(report.get('removed_created_files', []))} file")
    print(f"Konflik       : {len(report.get('conflicts', []))} file")
    if report.get("dry_run"):
        print("\nTambahkan --apply untuk menerapkan rollback ini.")


def _print_troubleshoot_human(report: dict) -> None:
    print(f"{APP_NAME} v{APP_VERSION} — Quick Troubleshooter (Read-Only)")
    print(f"Status Umum   : {report.get('status')} (peringatan={report.get('warnings', 0)})")
    for chk in report.get("checks", []):
        print(f"\n  [{chk['status']}] {chk['title']}")
        print(f"    Ringkasan : {chk['summary']}")
        print(f"    Perintah  : {chk['recommended_command']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="msshi-doctor",
        description=(
            f"{APP_NAME} v{APP_VERSION} — Diagnostik lokal, Online GitHub Diff "
            f"({UPSTREAM_FULL_NAME}), Safe Repair 6-Langkah, dan Rollback."
        ),
    )
    parser.add_argument("--version", action="store_true", help="Tampilkan versi MSSHI Doctor App.")
    parser.add_argument("--gui", action="store_true", help="Buka antarmuka desktop ringan (Tkinter).")
    parser.add_argument("--local", action="store_true", help="Jalankan diagnostik lokal read-only.")
    parser.add_argument("--online", action="store_true", help="Bandingkan file lokal vs GitHub resmi (read-only).")
    parser.add_argument(
        "--online-fix",
        action="store_true",
        help="Perbaikan selektif dari GitHub resmi (default --dry-run kecuali --apply diberikan).",
    )
    parser.add_argument(
        "--local-fix",
        action="store_true",
        help="Jalankan `msshi doctor --fix --json` lokal (wajib sertakan --apply).",
    )
    parser.add_argument("--receipts", action="store_true", help="Tampilkan daftar receipt & backup lokal.")
    parser.add_argument(
        "--rollback",
        metavar="RECEIPT_ID",
        help="Preview atau eksekusi rollback untuk receipt online-repair (sertakan --apply untuk menerapkan).",
    )
    parser.add_argument("--troubleshoot", action="store_true", help="Jalankan 5 pemeriksaan troubleshooter cepat.")
    parser.add_argument(
        "--check-app-update",
        action="store_true",
        help="Cek versi terbaru aplikasi MSSHI Doctor di zidanaisar624/MSSHI-DOCTOR (read-only).",
    )
    parser.add_argument(
        "--self-update",
        action="store_true",
        help="Preview (--dry-run) atau siapkan staging self-update aplikasi MSSHI Doctor (--apply).",
    )

    parser.add_argument("--dry-run", action="store_true", help="Mode preview tanpa menulis file.")
    parser.add_argument("--apply", action="store_true", help="Terapkan aksi perbaikan/rollback secara atomik.")
    parser.add_argument("--yes", action="store_true", help="Konfirmasi non-interaktif untuk otomatisasi.")
    parser.add_argument("--json", action="store_true", help="Cetak keluaran JSON terstruktur.")
    parser.add_argument("--ref", help="Target branch/tag/commit GitHub (default otomatis dari VERSION.json atau v3.2.1).")
    parser.add_argument("--scope", choices=("core", "full"), default="core", help="Cakupan audit file Zona 1.")
    parser.add_argument("--files", nargs="+", help="Daftar path relatif Zona 1 yang dipilih untuk --online-fix.")
    parser.add_argument("--target-root", help="Path eksplisit direktori instalasi MSSHI lokal.")
    parser.add_argument("--hermes-home", help="Path eksplisit folder HermesData lokal.")
    parser.add_argument("--msshi-home", help="Path eksplisit folder launcher MSSHI lokal.")
    parser.add_argument("--cache-dir", help="Direktori cache ETag GitHub.")
    parser.add_argument("--no-cache", action="store_true", help="Nonaktifkan cache ETag disk.")
    parser.add_argument("--no-fallback", action="store_true", help="Jangan gunakan fallback lokal saat offline.")
    parser.add_argument("--no-recheck", action="store_true", help="Lewati re-check `msshi doctor --json` pasca-repair.")
    parser.add_argument("--skip-cli", action="store_true", help="Untuk --local: baca metadata lokal tanpa memanggil CLI msshi.")
    return parser


def run_cli(argv: list[str] | None = None, *, client: GitHubClient | None = None) -> int:
    _configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        payload = {"app": APP_NAME, "version": APP_VERSION, "upstream_repo": UPSTREAM_FULL_NAME}
        if args.json:
            _emit_json(payload)
        else:
            print(f"{APP_NAME} v{APP_VERSION}")
        return EXIT_HEALTHY

    if args.gui:
        from .gui import launch_gui

        return launch_gui(
            hermes_home=args.hermes_home,
            msshi_home=args.msshi_home,
            target_root=args.target_root,
            ref=args.ref,
        )

    h_home = resolve_hermes_home(args.hermes_home)
    m_home = resolve_msshi_home(args.msshi_home)
    target_root, root_source = resolve_msshi_target_root(
        args.target_root, msshi_home=m_home, hermes_home=h_home
    )
    effective_ref = args.ref or detect_target_ref(target_root)
    cache_dir = None if args.no_cache else resolve_cache_dir(args.cache_dir)

    # Default action if invoked without explicit mode flags: read-only --local
    explicit_modes = any((
        args.local,
        args.online,
        args.online_fix,
        args.local_fix,
        args.receipts,
        bool(args.rollback),
        args.troubleshoot,
        args.check_app_update,
        args.self_update,
    ))
    if not explicit_modes:
        args.local = True

    try:
        if args.local:
            report = collect_local_snapshot(
                hermes_home=h_home,
                msshi_home=args.msshi_home,
                target_root=args.target_root,
                run_cli=not args.skip_cli,
            )
            if args.json:
                _emit_json(report)
            else:
                _print_local_human(report)
            return int(report.get("exit_code", EXIT_HEALTHY))

        if args.online:
            if target_root is None or not target_root.is_dir():
                err = {
                    "status": "ERROR",
                    "error": f"Target instalasi MSSHI lokal tidak ditemukan (source={root_source}). Gunakan --target-root.",
                    "exit_code": EXIT_CORE_ERROR,
                }
                if args.json:
                    _emit_json(err)
                else:
                    print(err["error"], file=sys.stderr)
                return EXIT_CORE_ERROR
            fallback_root = None if args.no_fallback else (h_home / "msshi-upstream")
            report = run_online_diff(
                target_root,
                ref=effective_ref,
                hermes_home=h_home,
                scope=args.scope,
                client=client,
                cache_dir=cache_dir,
                local_fallback_root=fallback_root,
            )
            if args.json:
                _emit_json(report)
            else:
                _print_online_human(report)
            return int(report.get("exit_code", EXIT_HEALTHY))

        if args.online_fix:
            if target_root is None or not target_root.is_dir():
                err = {
                    "status": "ERROR",
                    "error": "Target instalasi MSSHI lokal tidak ditemukan. Gunakan --target-root.",
                    "exit_code": EXIT_CORE_ERROR,
                }
                if args.json:
                    _emit_json(err)
                else:
                    print(err["error"], file=sys.stderr)
                return EXIT_CORE_ERROR
            selected = _parse_files_arg(args.files)
            do_apply = bool(args.apply and not args.dry_run)
            if do_apply:
                report = apply_online_repair(
                    target_root,
                    h_home,
                    ref=effective_ref,
                    scope=args.scope,
                    selected_files=selected,
                    client=client,
                    cache_dir=cache_dir,
                    run_recheck=not args.no_recheck,
                    msshi_home=args.msshi_home,
                )
            else:
                report = plan_online_repair(
                    target_root,
                    h_home,
                    ref=effective_ref,
                    scope=args.scope,
                    selected_files=selected,
                    client=client,
                    cache_dir=cache_dir,
                )
            if args.json:
                _emit_json(report)
            else:
                _print_repair_human(report)
            return int(report.get("exit_code", EXIT_HEALTHY))

        if args.local_fix:
            if not args.apply or args.dry_run:
                preview = {
                    "operation": "local_doctor_fix_preview",
                    "dry_run": True,
                    "applied": False,
                    "command": "msshi doctor --fix --json",
                    "note": "Tambahkan --apply untuk mengeksekusi perbaikan lokal aman milik MSSHI CLI.",
                    "exit_code": EXIT_NEEDS_ATTENTION,
                }
                if args.json:
                    _emit_json(preview)
                else:
                    print(preview["note"])
                return EXIT_NEEDS_ATTENTION
            res = run_local_doctor_fix(msshi_home=args.msshi_home, hermes_home=h_home)
            if args.json:
                _emit_json(res)
            else:
                repair = (res.get("data") or {}).get("repair") or {}
                if repair.get("applied"):
                    print(f"Perbaikan lokal memulihkan {repair.get('files_restored', 0)} file.")
                elif repair.get("applied") is False:
                    print("Perbaikan lokal tidak mengubah file.")
                elif res.get("ok"):
                    print("Pemeriksaan lokal selesai.")
                if not res.get("ok"):
                    print(res.get("error"))
            return int(res.get("exit_code", EXIT_CORE_ERROR))

        if args.receipts:
            items = discover_receipts(h_home)
            payload = {
                "schema_version": 1,
                "hermes_home": str(h_home),
                "count": len(items),
                "receipts": items,
                "exit_code": EXIT_HEALTHY,
            }
            if args.json:
                _emit_json(payload)
            else:
                _print_receipts_human(items, h_home)
            return EXIT_HEALTHY

        if args.rollback:
            do_apply = bool(args.apply and not args.dry_run)
            report = rollback_online_repair(h_home, args.rollback, apply=do_apply)
            if args.json:
                _emit_json(report)
            else:
                _print_rollback_human(report)
            return int(report.get("exit_code", EXIT_HEALTHY))

        if args.troubleshoot:
            report = run_all_troubleshooter_checks(hermes_home=h_home, msshi_home=args.msshi_home)
            exit_code = EXIT_HEALTHY if report["status"] == "HEALTHY" else EXIT_NEEDS_ATTENTION
            report["exit_code"] = exit_code
            if args.json:
                _emit_json(report)
            else:
                _print_troubleshoot_human(report)
            return exit_code

        if args.check_app_update:
            report = check_app_update(cache_dir=cache_dir)
            if args.json:
                _emit_json(report)
            else:
                print(f"{APP_NAME} v{APP_VERSION} — Cek Self-Update ({report.get('app_repo')})")
                print(f"Koneksi       : {report.get('connection_status')} (cache={report.get('from_cache')})")
                print(f"Versi Lokal   : v{report.get('current_version')}")
                print(f"Versi GitHub  : v{report.get('latest_version')} ({report.get('latest_tag')})")
                if report.get("update_available"):
                    print(f"Status        : PEMBARUAN TERSEDIA -> {report.get('release_url')}")
                    if report.get("changelog"):
                        print("\n[Ringkasan Changelog]\n" + report["changelog"][:800])
                    print("\nJalankan --self-update --dry-run untuk pratinjau pembaruan aman.")
                elif report.get("error_message"):
                    print(f"Catatan       : {report.get('error_message')}")
                else:
                    print("Status        : Sudah versi terbaru.")
            return int(report.get("exit_code", EXIT_HEALTHY))

        if args.self_update:
            do_apply = bool(args.apply and not args.dry_run)
            report = run_self_update(apply=do_apply, cache_dir=cache_dir)
            if args.json:
                _emit_json(report)
            else:
                print(f"{APP_NAME} v{APP_VERSION} — Self-Update ({'APPLY' if do_apply else 'DRY-RUN'})")
                print(f"Repo Aplikasi : {report.get('app_repo')} ({report.get('connection_status')})")
                print(f"Versi         : v{report.get('current_version')} -> v{report.get('latest_version')}")
                print(f"Catatan       : {report.get('note') or report.get('error_message') or '-'}")
                if report.get("handoff_script"):
                    print(f"Handoff Cmd   : {report.get('handoff_script')}")
            return int(report.get("exit_code", EXIT_HEALTHY))

    except (OSError, ValueError) as exc:
        err_payload = {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "exit_code": EXIT_CORE_ERROR,
        }
        if args.json:
            _emit_json(err_payload)
        else:
            print(f"[{type(exc).__name__}] {exc}", file=sys.stderr)
        return EXIT_CORE_ERROR

    return EXIT_HEALTHY


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
