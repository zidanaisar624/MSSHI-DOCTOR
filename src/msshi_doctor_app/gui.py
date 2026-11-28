"""Modern-Minimalist lightweight Desktop GUI for MSSHI Doctor App (Pure Tkinter/ttk)."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from .constants import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_REF,
    PALETTE,
    SAWERIA_URL,
    UPSTREAM_FULL_NAME,
    resolve_assets_dir,
    resolve_cache_dir,
)
from .diff_engine import run_online_diff
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
from .safety import is_zone3_protected, reject_links
from .self_update import check_app_update
from .troubleshooter import launch_hermes_software_rendering, run_all_troubleshooter_checks


def _enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _fmt_bytes(num_bytes: int | None) -> str:
    n = int(num_bytes or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


_WRITE_JOBS = {"repair", "local_fix", "rollback"}


class SafeRepairConfirmModal(tk.Toplevel):
    """Panel 3: Confirmation & Progress Modal for 6-Step Atomic Online Safe Repair."""

    def __init__(
        self,
        parent: MSSSIDoctorGUI,
        plan: dict,
        *,
        target_root: Path,
        hermes_home: Path,
        msshi_home: str | Path | None,
        selected_files: list[str],
        ref: str,
        scope: str,
        cache_dir: Path,
        on_success,
    ) -> None:
        super().__init__(parent)
        self.parent_app = parent
        self.plan = plan
        self.target_root = target_root
        self.hermes_home = hermes_home
        self.msshi_home = msshi_home
        self.selected_files = selected_files
        self.ref = ref
        self.scope = scope
        self.cache_dir = cache_dir
        self.on_success = on_success
        self._running = False

        self.title("Konfirmasi Perbaikan Aman (6-Langkah) — MSSHI Doctor")
        self.configure(bg=PALETTE["bg"])
        self.geometry("760x580")
        self.minsize(680, 500)
        self.transient(parent)
        self.grab_set()

        ico_path = resolve_assets_dir() / "msshi-doctor.ico"
        if os.name == "nt" and ico_path.is_file():
            try:
                self.iconbitmap(str(ico_path))
            except tk.TclError:
                pass

        self.protocol("WM_DELETE_WINDOW", lambda: None if self._running else self.destroy())
        self._build_ui()

    def _build_ui(self) -> None:
        p = PALETTE
        wrap = tk.Frame(self, bg=p["bg"], padx=20, pady=16)
        wrap.pack(fill="both", expand=True)

        tk.Label(
            wrap,
            text="Preview & Konfirmasi Perbaikan Aman (Online GitHub)",
            font=("Segoe UI", 14, "bold"),
            fg=p["text"],
            bg=p["bg"],
        ).pack(anchor="w")

        tk.Label(
            wrap,
            text=(
                f"Sumber resmi: {self.plan.get('github_repo')} @ {self.plan.get('github_ref')} "
                f"| Commit terpin: {(self.plan.get('commit_sha') or '-')[:12]}"
            ),
            font=("Segoe UI", 9),
            fg=p["cyan"],
            bg=p["bg"],
        ).pack(anchor="w", pady=(4, 10))

        # Info box
        info = tk.Frame(wrap, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1, padx=12, pady=10)
        info.pack(fill="x", pady=(0, 10))
        tk.Label(
            info,
            text=f"Target Lokal : {self.plan.get('target_root')}",
            font=("Consolas", 9),
            fg=p["text"],
            bg=p["surface"],
        ).pack(anchor="w")
        tk.Label(
            info,
            text=f"Backup Atomik: {self.plan.get('backup_dir_preview')}",
            font=("Consolas", 9),
            fg=p["text_muted"],
            bg=p["surface"],
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            info,
            text=(
                f"Total File   : {self.plan.get('planned_count', 0)} file Zona 1 "
                f"(estimasi unduh {_fmt_bytes(self.plan.get('total_download_bytes'))})"
            ),
            font=("Segoe UI", 9, "bold"),
            fg=p["emerald"],
            bg=p["surface"],
        ).pack(anchor="w", pady=(4, 0))

        # Safety guarantee banner
        guard = tk.Frame(wrap, bg=p["emerald_soft"], highlightbackground=p["emerald_dark"], highlightthickness=1, padx=12, pady=8)
        guard.pack(fill="x", pady=(0, 10))
        tk.Label(
            guard,
            text="Jaminan Perlindungan Zona 2 & Zona 3:",
            font=("Segoe UI", 9, "bold"),
            fg=p["emerald"],
            bg=p["emerald_soft"],
        ).pack(anchor="w")
        tk.Label(
            guard,
            text=f"• Zona 2: {self.plan.get('zone2_protected_guarantee')}",
            font=("Segoe UI", 8),
            fg=p["text"],
            bg=p["emerald_soft"],
            wraplength=690,
            justify="left",
        ).pack(anchor="w")
        tk.Label(
            guard,
            text=f"• Zona 3: {self.plan.get('zone3_protected_guarantee')}",
            font=("Segoe UI", 8),
            fg=p["text"],
            bg=p["emerald_soft"],
            wraplength=690,
            justify="left",
        ).pack(anchor="w")

        # File list preview
        list_frame = tk.Frame(wrap, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1)
        list_frame.pack(fill="both", expand=True, pady=(0, 10))
        cols = ("status", "path", "size")
        tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=9, style="Dark.Treeview")
        tree.heading("status", text="Status")
        tree.heading("path", text="Path File Zona 1")
        tree.heading("size", text="Ukuran")
        tree.column("status", width=105, anchor="center")
        tree.column("path", width=480, anchor="w")
        tree.column("size", width=90, anchor="e")
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for row in self.plan.get("planned_files", []):
            tree.insert("", "end", values=(row["status"], row["rel_path"], _fmt_bytes(row.get("upstream_size"))))

        # Status & log label
        self.status_var = tk.StringVar(
            value="Siap menerapkan: Backup -> Download Terpin -> Verifikasi SHA -> Receipt JSON -> Re-check."
        )
        tk.Label(
            wrap,
            textvariable=self.status_var,
            font=("Segoe UI", 9),
            fg=p["text_muted"],
            bg=p["bg"],
            wraplength=710,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        # Action buttons
        btns = tk.Frame(wrap, bg=p["bg"])
        btns.pack(fill="x")
        self.cancel_btn = tk.Button(
            btns,
            text="Batal",
            command=self.destroy,
            bg=p["surface_alt"],
            fg=p["text"],
            activebackground=p["surface_hover"],
            activeforeground=p["text"],
            relief="flat",
            padx=16,
            pady=8,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        self.cancel_btn.pack(side="right", padx=(8, 0))

        self.apply_btn = tk.Button(
            btns,
            text="Jalankan Perbaikan Aman",
            command=self._execute_repair,
            bg=p["emerald"],
            fg="#041510",
            activebackground=p["emerald_dark"],
            activeforeground="#FFFFFF",
            relief="flat",
            padx=18,
            pady=8,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            state="normal" if self.plan.get("can_apply") else "disabled",
        )
        self.apply_btn.pack(side="right")

    def _execute_repair(self) -> None:
        if self._running:
            return
        self._running = True
        self.apply_btn.configure(state="disabled", text="Memproses 6-Langkah...")
        self.cancel_btn.configure(state="disabled")
        self.status_var.set("Mengunduh selektif, memverifikasi SHA, membuat backup & receipt...")

        self.parent_app._background("repair", lambda: apply_online_repair(
            self.target_root, self.hermes_home, ref=self.ref, scope=self.scope,
            selected_files=self.selected_files, cache_dir=self.cache_dir,
            expected_local_sha256={r["rel_path"]: r["local_sha256"] for r in self.plan["planned_files"]},
            run_recheck=True, msshi_home=self.msshi_home,
        ), lambda result: self._on_done(result, None), lambda exc: self._on_done(None, exc))

    def _on_done(self, res: dict | None, err: Exception | None) -> None:
        self._running = False
        self.cancel_btn.configure(state="normal", text="Tutup")
        if err is not None:
            self.status_var.set(f"Dibatalkan secara aman (rollback otomatis jika sempat menulis): {err}")
            messagebox.showerror("Perbaikan Dibatalkan", str(err), parent=self)
            self.apply_btn.configure(state="disabled", text="Buat pratinjau baru")
            return
        assert res is not None
        if res.get("state") != "applied":
            messagebox.showwarning("Belum ada perbaikan", "Pemeriksaan perlu diulang. Buka kembali perbandingan GitHub.", parent=self)
            self.destroy()
            return
        rid = res.get("receipt_id") or "-"
        repaired = res.get("files_repaired", 0)
        self.status_var.set(f"Selesai! {repaired} file inti diperbaiki. Receipt: {rid}")
        messagebox.showinfo(
            "Perbaikan Aman Berhasil",
            (
                f"{repaired} file inti MSSHI berhasil diperbaiki & diverifikasi SHA.\n\n"
                f"Receipt ID: {rid}\n"
                f"Backup: {res.get('backup_dir')}\n\n"
                "Zona 2 (SOUL/config/.env) & Zona 3 tidak tersentuh."
            ),
            parent=self,
        )
        if callable(self.on_success):
            self.on_success(res)
        self.destroy()


class MSSSIDoctorGUI(tk.Tk):
    """Main lightweight MSSHI Doctor desktop application."""

    def __init__(
        self,
        *,
        hermes_home: str | Path | None = None,
        msshi_home: str | Path | None = None,
        target_root: str | Path | None = None,
        ref: str | None = None,
        auto_close_ms: int | None = None,
    ) -> None:
        super().__init__()
        self.hermes_home = resolve_hermes_home(hermes_home)
        self._launcher_home = msshi_home
        self.msshi_home = resolve_msshi_home(msshi_home)
        resolved_root, _ = resolve_msshi_target_root(
            target_root, msshi_home=self.msshi_home, hermes_home=self.hermes_home
        )
        self.target_root = resolved_root
        self.default_ref = ref or detect_target_ref(resolved_root)
        self.cache_dir = resolve_cache_dir()
        self.assets_dir = resolve_assets_dir()

        self._images: dict[str, tk.PhotoImage] = {}
        self._nav_buttons: dict[str, tk.Button] = {}
        self._pages: dict[str, tk.Frame] = {}
        self._active_page = ""
        self._working: set[str] = set()
        self._scanned_options = None
        self._latest_repair_id = None

        self._local_snapshot: dict | None = None
        self._diff_report: dict | None = None
        self._checked_paths: set[str] = set()
        self._diff_filter = tk.StringVar(value="ISSUES")
        self._ref_var = tk.StringVar(value=self.default_ref)
        self._scope_var = tk.StringVar(value="core")
        self._receipts_list: list[dict] = []
        self._selected_receipt: dict | None = None

        self.title(f"{APP_NAME} v{APP_VERSION} — Diagnostik & Safe Repair MSSHI")
        self.configure(bg=PALETTE["bg"])
        self.geometry("1160x730")
        self.minsize(960, 620)

        self._load_branding_assets()
        self._configure_styles()
        self._build_layout()
        for selector in (self._ref_var, self._scope_var):
            selector.trace_add("write", lambda *_: self._update_repair_button_state())
        self._show_page("guide")

        # Kick off lightweight background local refresh immediately
        self.after(80, self.refresh_local_dashboard)
        self.protocol("WM_DELETE_WINDOW", self._request_close)

        if auto_close_ms is not None and auto_close_ms > 0:
            self.after(auto_close_ms, self.destroy)

    def _load_branding_assets(self) -> None:
        ico = self.assets_dir / "msshi-doctor.ico"
        if os.name == "nt" and ico.is_file():
            try:
                self.iconbitmap(str(ico))
            except tk.TclError:
                pass

        icon_png = self.assets_dir / "msshi-doctor-icon-64.png"
        if icon_png.is_file():
            try:
                photo = tk.PhotoImage(file=str(icon_png))
                self._images["icon_64"] = photo
                self.iconphoto(True, photo)
                self._images["icon_32"] = photo.subsample(2, 2)
            except tk.TclError:
                pass

        banner_png = self.assets_dir / "msshi-doctor-banner-ui.png"
        if banner_png.is_file():
            try:
                banner = tk.PhotoImage(file=str(banner_png))
                self._images["banner"] = banner
            except tk.TclError:
                pass

    def _configure_styles(self) -> None:
        p = PALETTE
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Dark.Treeview",
            background=p["surface"],
            foreground=p["text"],
            fieldbackground=p["surface"],
            bordercolor=p["border"],
            borderwidth=0,
            rowheight=28,
            font=("Segoe UI", 9),
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", p["cyan_soft"])],
            foreground=[("selected", p["cyan"])],
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=p["surface_alt"],
            foreground=p["text_muted"],
            relief="flat",
            font=("Segoe UI", 9, "bold"),
        )
        style.map("Dark.Treeview.Heading", background=[("active", p["surface_hover"])])

        style.configure(
            "Dark.TCombobox",
            fieldbackground=p["surface_alt"],
            background=p["surface_alt"],
            foreground=p["text"],
            bordercolor=p["border_strong"],
            arrowcolor=p["cyan"],
        )
        style.configure(
            "Emerald.Horizontal.TProgressbar",
            troughcolor=p["surface_alt"],
            background=p["emerald"],
            bordercolor=p["border"],
            lightcolor=p["emerald"],
            darkcolor=p["emerald_dark"],
        )

    def _build_layout(self) -> None:
        p = PALETTE
        # Sidebar
        sidebar = tk.Frame(self, bg=p["sidebar"], width=235)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=p["sidebar"], padx=16, pady=18)
        brand.pack(fill="x")
        if "icon_32" in self._images:
            tk.Label(brand, image=self._images["icon_32"], bg=p["sidebar"]).pack(side="left", padx=(0, 10))
        brand_txt = tk.Frame(brand, bg=p["sidebar"])
        brand_txt.pack(side="left", fill="x")
        tk.Label(brand_txt, text=APP_NAME, font=("Segoe UI", 12, "bold"), fg=p["text"], bg=p["sidebar"]).pack(anchor="w")
        tk.Label(brand_txt, text=f"v{APP_VERSION} • Zero Paid Calls", font=("Segoe UI", 8), fg=p["emerald"], bg=p["sidebar"]).pack(anchor="w")

        tk.Frame(sidebar, bg=p["border"], height=1).pack(fill="x", padx=14, pady=(0, 12))

        nav_items = [
            ("guide", "Mulai di sini"),
            ("dashboard", "Hasil pemeriksaan lokal"),
            ("online_diff", "Bandingkan dengan GitHub"),
            ("receipts", "Riwayat dan pemulihan"),
            ("troubleshooter", "Cari penyebab masalah"),
        ]
        for key, label in nav_items:
            btn = tk.Button(
                sidebar,
                text=f"  {label}",
                anchor="w",
                command=lambda k=key: self._show_page(k),
                bg=p["sidebar"],
                fg=p["text_muted"],
                activebackground=p["surface_alt"],
                activeforeground=p["text"],
                relief="flat",
                padx=14,
                pady=11,
                font=("Segoe UI", 9, "bold"),
                cursor="hand2",
            )
            btn.pack(fill="x", padx=10, pady=3)
            self._nav_buttons[key] = btn

        # Footer safety card in sidebar
        spacer = tk.Frame(sidebar, bg=p["sidebar"])
        spacer.pack(fill="both", expand=True)

        safety_box = tk.Frame(sidebar, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1, padx=12, pady=10)
        safety_box.pack(fill="x", padx=12, pady=(10, 8))
        tk.Label(safety_box, text="Mode Aman Aktif", font=("Segoe UI", 9, "bold"), fg=p["emerald"], bg=p["surface"]).pack(anchor="w")
        tk.Label(
            safety_box,
            text="• 0 rahasia dibaca/dikirim\n• Zona 2 & 3 terlindungi\n• Model-agnostic & hemat RAM",
            font=("Segoe UI", 8),
            fg=p["text_muted"],
            bg=p["surface"],
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        footer_actions = tk.Frame(sidebar, bg=p["sidebar"])
        footer_actions.pack(fill="x", padx=12, pady=(0, 12))
        tk.Button(
            footer_actions,
            text="Cek Update Doctor",
            command=self._check_app_self_update,
            bg=p["surface_alt"],
            fg=p["cyan"],
            activebackground=p["surface_hover"],
            activeforeground=p["text"],
            relief="flat",
            pady=6,
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
        ).pack(fill="x", pady=(0, 6))
        tk.Button(
            footer_actions,
            text="Dukung via Saweria",
            command=self._open_saweria,
            bg=p["amber_soft"],
            fg=p["amber"],
            activebackground=p["surface_hover"],
            activeforeground=p["text"],
            relief="flat",
            pady=6,
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
        ).pack(fill="x")

        # Main workspace container
        main_col = tk.Frame(self, bg=p["bg"])
        main_col.pack(side="right", fill="both", expand=True)

        # Top subtle status strip
        topbar = tk.Frame(main_col, bg=p["surface"], height=44)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)
        self.top_status_var = tk.StringVar(value="Memuat diagnostik lokal read-only...")
        tk.Label(
            topbar,
            textvariable=self.top_status_var,
            font=("Segoe UI", 9),
            fg=p["text_muted"],
            bg=p["surface"],
        ).pack(side="left", padx=18)

        self.overall_badge = tk.Label(
            topbar,
            text="CHECKING",
            font=("Segoe UI", 8, "bold"),
            fg=p["cyan"],
            bg=p["cyan_soft"],
            padx=10,
            pady=4,
        )
        self.overall_badge.pack(side="right", padx=18)

        tk.Frame(main_col, bg=p["border"], height=1).pack(fill="x")

        self.content_area = tk.Frame(main_col, bg=p["bg"])
        self.content_area.pack(fill="both", expand=True)

        self._pages["guide"] = self._build_guide_page(self.content_area)
        self._pages["dashboard"] = self._build_dashboard_page(self.content_area)
        self._pages["online_diff"] = self._build_online_diff_page(self.content_area)
        self._pages["receipts"] = self._build_receipts_page(self.content_area)
        self._pages["troubleshooter"] = self._build_troubleshooter_page(self.content_area)

    def _show_page(self, key: str) -> None:
        p = PALETTE
        previous = self._active_page
        self._active_page = key
        if key != previous:
            if key == "receipts":
                self.refresh_receipts()
            elif key == "troubleshooter":
                self.refresh_troubleshooter()
        for name, frame in self._pages.items():
            if name == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for name, btn in self._nav_buttons.items():
            if name == key:
                btn.configure(bg=p["surface_alt"], fg=p["emerald"])
            else:
                btn.configure(bg=p["sidebar"], fg=p["text_muted"])

    def _request_close(self) -> None:
        if self._working.intersection(_WRITE_JOBS):
            self.top_status_var.set("Tunggu sampai penulisan dan penyimpanan bukti perbaikan selesai.")
            return
        self.destroy()

    def _background(self, key, work, on_result, on_error=None) -> None:
        """Perform file/network work off-thread and consume the result only on Tk's thread."""
        if key in self._working:
            return
        self._working.add(key)
        result = SimpleQueue()

        def worker():
            try:
                value = work()
            except Exception as exc:
                result.put((None, exc))
            else:
                result.put((value, None))

        def check():
            try:
                value, error = result.get_nowait()
            except Empty:
                self.after(70, check)
                return
            self._working.discard(key)
            if error is None:
                on_result(value)
            elif on_error is not None:
                on_error(error)
            else:
                self.top_status_var.set(f"Pemeriksaan gagal. {error}")

        threading.Thread(target=worker, daemon=key not in _WRITE_JOBS).start()
        self.after(10, check)

    def _build_guide_page(self, parent: tk.Widget) -> tk.Frame:
        p = PALETTE
        page = tk.Frame(parent, bg=p["bg"], padx=24, pady=24)
        tk.Label(page, text="Periksa instalasi MSSHI", font=("Segoe UI", 20, "bold"),
                 bg=p["bg"], fg=p["text"]).pack(anchor="w")
        card = tk.Frame(page, bg=p["surface"], padx=18, pady=16,
                        highlightbackground=p["border"], highlightthickness=1)
        card.pack(fill="x", pady=(12, 14))
        self.guide_status = tk.StringVar(value="Sedang memeriksa instalasi lokal…")
        tk.Label(card, textvariable=self.guide_status, font=("Segoe UI", 11, "bold"),
                 wraplength=600, justify="left", bg=p["surface"], fg=p["text"]).pack(anchor="w")
        self.guide_target = tk.StringVar(value="")
        tk.Label(card, textvariable=self.guide_target, font=("Segoe UI", 9), wraplength=600,
                 justify="left", bg=p["surface"], fg=p["text_muted"]).pack(anchor="w", pady=(4, 6))
        actions = tk.Frame(card, bg=p["surface"])
        actions.pack(anchor="w")
        self.guide_next = tk.Button(actions, text="Bandingkan file dengan GitHub", state="disabled",
                                    command=self._guide_compare, bg=p["emerald"], fg="#081915",
                                    relief="flat", padx=12, pady=7, font=("Segoe UI", 10, "bold"))
        self.guide_next.pack(side="left", padx=(0, 12))
        tk.Button(actions, text="Pilih folder MSSHI…", command=self._choose_target, relief="flat",
                  bg=p["surface_alt"], fg=p["text"], padx=10, pady=7).pack(side="left")

        tk.Label(page, text="Apa yang ingin kamu lakukan?", font=("Segoe UI", 12, "bold"),
                 bg=p["bg"], fg=p["text"]).pack(anchor="w", pady=(4, 5))
        for title, detail, dest in (
            ("Lihat hasil pemeriksaan", "Lihat status profil bot, skill, dan alat lokal.", "dashboard"),
            ("Hermes sulit dibuka atau lambat", "Periksa layar abu-abu, gateway, dan pemuatan skill.", "troubleshooter"),
            ("Batalkan perbaikan sebelumnya", "Pilih bukti perbaikan untuk melihat pratinjau pemulihan.", "receipts"),
        ):
            row = tk.Frame(page, bg=p["bg"])
            row.pack(fill="x", pady=4)
            tk.Button(row, text=title, command=lambda key=dest: self._show_page(key),
                      anchor="w", width=33, bg=p["surface_alt"], fg=p["cyan"], relief="flat",
                      padx=9, pady=6, font=("Segoe UI", 9, "bold")).pack(side="left")
            tk.Label(row, text=detail, wraplength=320, justify="left", bg=p["bg"],
                     fg=p["text_muted"], font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))
        return page

    def _guide_compare(self) -> None:
        self._show_page("online_diff")
        self.run_online_check()

    def _choose_target(self) -> None:
        if self._working:
            self.top_status_var.set("Tunggu hingga pemeriksaan selesai sebelum mengganti folder.")
            return
        name = filedialog.askdirectory(parent=self, title="Pilih folder instalasi MSSHI", mustexist=True)
        if not name:
            return
        try:
            if is_zone3_protected(name):
                raise ValueError("Pilih folder program MSSHI yang berada di luar direktori pribadi.")
            target = reject_links(Path(name))
            markers = ("VERSION.json", "scripts/msshi_cli.py", "INSTALL_FOR_AGENTS.md")
            if sum(reject_links(target / m).is_file() for m in markers) < 2:
                raise ValueError("Pilih folder utama MSSHI. Doctor mengenalinya dari VERSION.json, "
                                 "scripts/msshi_cli.py dan INSTALL_FOR_AGENTS.md.")
        except (OSError, ValueError) as exc:
            messagebox.showwarning("Folder belum sesuai", str(exc), parent=self)
            return
        self.target_root = target
        self._ref_var.set(detect_target_ref(target))
        self._diff_report = None
        self._checked_paths.clear()
        self._populate_diff_tree()
        self._update_repair_button_state()
        self.refresh_local_dashboard()

    # ------------------------------------------------------------------
    # PANEL 1: DASHBOARD KESEHATAN
    # ------------------------------------------------------------------
    def _build_dashboard_page(self, parent: tk.Widget) -> tk.Frame:
        p = PALETTE
        page = tk.Frame(parent, bg=p["bg"], padx=18, pady=14)

        # Hero row: Banner + Summary controls
        hero = tk.Frame(page, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1, padx=14, pady=12)
        hero.pack(fill="x", pady=(0, 12))

        if "banner" in self._images:
            banner_lbl = tk.Label(hero, image=self._images["banner"], bg=p["surface"], bd=0)
            banner_lbl.pack(side="left", padx=(0, 16))

        hero_right = tk.Frame(hero, bg=p["surface"])
        hero_right.pack(side="left", fill="both", expand=True)
        tk.Label(
            hero_right,
            text="Dashboard Kesehatan Ekosistem MSSHI & Hermes",
            font=("Segoe UI", 13, "bold"),
            fg=p["text"],
            bg=p["surface"],
        ).pack(anchor="w")

        self.hero_paths_var = tk.StringVar(value=f"Hermes Home: {self.hermes_home}\nTarget Ref: {self.default_ref}")
        tk.Label(
            hero_right,
            textvariable=self.hero_paths_var,
            font=("Consolas", 8),
            fg=p["text_muted"],
            bg=p["surface"],
            justify="left",
        ).pack(anchor="w", pady=(6, 10))

        # Hardware RAM bar
        self.hw_label_var = tk.StringVar(value="Membaca telemetri RAM, CPU, dan Disk...")
        tk.Label(
            hero_right,
            textvariable=self.hw_label_var,
            font=("Segoe UI", 9),
            fg=p["cyan"],
            bg=p["surface"],
            justify="left",
        ).pack(anchor="w")

        self.ram_bar = ttk.Progressbar(
            hero_right, orient="horizontal", mode="determinate", maximum=100, style="Emerald.Horizontal.TProgressbar"
        )
        self.ram_bar.pack(fill="x", pady=(4, 10))

        btn_row = tk.Frame(hero_right, bg=p["surface"])
        btn_row.pack(anchor="w")
        tk.Button(
            btn_row,
            text="Segarkan Diagnostik",
            command=self.refresh_local_dashboard,
            bg=p["cyan"],
            fg="#05131F",
            activebackground=p["cyan_dark"],
            activeforeground="#FFFFFF",
            relief="flat",
            padx=12,
            pady=6,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            btn_row,
            text="Buka Online GitHub Diff",
            command=lambda: self._show_page("online_diff"),
            bg=p["emerald"],
            fg="#041510",
            activebackground=p["emerald_dark"],
            activeforeground="#FFFFFF",
            relief="flat",
            padx=12,
            pady=6,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left", padx=(0, 8))

        tk.Button(
            btn_row,
            text="Perbaikan Lokal (msshi doctor --fix)",
            command=self._prompt_local_doctor_fix,
            bg=p["surface_alt"],
            fg=p["text"],
            activebackground=p["surface_hover"],
            activeforeground=p["text"],
            relief="flat",
            padx=12,
            pady=6,
            font=("Segoe UI", 9),
            cursor="hand2",
        ).pack(side="left")

        # 5 Honest Status Cards
        tk.Label(
            page,
            text="5 Status Jujur (Tanpa Menggabungkan ke Label READY Palsu)",
            font=("Segoe UI", 10, "bold"),
            fg=p["text"],
            bg=p["bg"],
        ).pack(anchor="w", pady=(0, 6))

        self.status_cards_frame = tk.Frame(page, bg=p["bg"])
        self.status_cards_frame.pack(fill="x", pady=(0, 12))
        self._status_card_widgets: list[tuple[tk.Label, tk.Label, tk.Label]] = []
        for idx in range(5):
            card = tk.Frame(
                self.status_cards_frame,
                bg=p["surface"],
                highlightbackground=p["border"],
                highlightthickness=1,
                padx=10,
                pady=8,
            )
            card.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 6, 0))
            self.status_cards_frame.columnconfigure(idx, weight=1, uniform="five")
            title_lbl = tk.Label(card, text=f"{idx + 1}. ...", font=("Segoe UI", 8, "bold"), fg=p["text"], bg=p["surface"])
            title_lbl.pack(anchor="w")
            badge_lbl = tk.Label(card, text="LOADING", font=("Segoe UI", 7, "bold"), fg=p["cyan"], bg=p["cyan_soft"], padx=6, pady=2)
            badge_lbl.pack(anchor="w", pady=(4, 4))
            detail_lbl = tk.Label(card, text="...", font=("Segoe UI", 8), fg=p["text_muted"], bg=p["surface"], wraplength=150, justify="left")
            detail_lbl.pack(anchor="w", fill="x")
            self._status_card_widgets.append((title_lbl, badge_lbl, detail_lbl))

        # 6 Native Profiles Table
        tk.Label(
            page,
            text="Kesehatan 6 Profil Native (SOUL.md Pasif, Skills, MSSHITrim, Interaksi & Kesiapan)",
            font=("Segoe UI", 10, "bold"),
            fg=p["text"],
            bg=p["bg"],
        ).pack(anchor="w", pady=(0, 6))

        prof_box = tk.Frame(page, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1)
        prof_box.pack(fill="both", expand=True)
        cols = ("profile", "role", "skills", "soul", "trim", "interaction", "readiness")
        self.profiles_tree = ttk.Treeview(prof_box, columns=cols, show="headings", style="Dark.Treeview")
        headings = {
            "profile": ("Profil", 95),
            "role": ("Peran", 100),
            "skills": ("Skill", 60),
            "soul": ("SOUL.md (UTF-8/NUL)", 145),
            "trim": ("MSSHITrim", 110),
            "interaction": ("Interaksi", 110),
            "readiness": ("Advisor / Council (Lokal)", 250),
        }
        for col, (label, width) in headings.items():
            self.profiles_tree.heading(col, text=label)
            self.profiles_tree.column(col, width=width, anchor="w" if col in ("profile", "role", "readiness") else "center")
        vsb = ttk.Scrollbar(prof_box, orient="vertical", command=self.profiles_tree.yview)
        self.profiles_tree.configure(yscrollcommand=vsb.set)
        self.profiles_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        return page

    # ------------------------------------------------------------------
    # PANEL 2: ONLINE GITHUB SYNC & DIFF
    # ------------------------------------------------------------------
    def _build_online_diff_page(self, parent: tk.Widget) -> tk.Frame:
        p = PALETTE
        page = tk.Frame(parent, bg=p["bg"], padx=18, pady=14)

        # Header & Controls
        ctrl = tk.Frame(page, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1, padx=14, pady=12)
        ctrl.pack(fill="x", pady=(0, 10))

        top_row = tk.Frame(ctrl, bg=p["surface"])
        top_row.pack(fill="x")
        tk.Label(
            top_row,
            text="Bandingkan file program dengan GitHub",
            font=("Segoe UI", 12, "bold"),
            fg=p["text"],
            bg=p["surface"],
        ).pack(side="left")

        self.online_conn_badge = tk.Label(
            top_row,
            text="BELUM DIPERIKSA",
            font=("Segoe UI", 8, "bold"),
            fg=p["text_muted"],
            bg=p["surface_alt"],
            padx=10,
            pady=4,
        )
        self.online_conn_badge.pack(side="right")

        mid_row = tk.Frame(ctrl, bg=p["surface"])
        mid_row.pack(fill="x", pady=(10, 4))
        tk.Label(mid_row, text="Versi sumber", font=("Segoe UI", 9), fg=p["text_muted"], bg=p["surface"]).pack(side="left")
        ref_combo = ttk.Combobox(
            mid_row,
            textvariable=self._ref_var,
            values=tuple(dict.fromkeys((self.default_ref, DEFAULT_REF, "v3.2.1", "v3.1.2", "main"))),
            width=14,
            style="Dark.TCombobox",
        )
        ref_combo.pack(side="left", padx=(6, 14))

        tk.Label(mid_row, text="Cakupan:", font=("Segoe UI", 9), fg=p["text_muted"], bg=p["surface"]).pack(side="left")
        scope_combo = ttk.Combobox(
            mid_row, textvariable=self._scope_var, values=("core", "full"), state="readonly", width=8, style="Dark.TCombobox"
        )
        scope_combo.pack(side="left", padx=(6, 14))

        self.check_online_btn = tk.Button(
            mid_row,
            text="Cek ke GitHub Sekarang",
            command=self.run_online_check,
            bg=p["cyan"],
            fg="#05131F",
            activebackground=p["cyan_dark"],
            activeforeground="#FFFFFF",
            relief="flat",
            padx=14,
            pady=6,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        self.check_online_btn.pack(side="left", padx=(0, 8))

        self.repair_selected_btn = tk.Button(
            mid_row,
            text="Preview & Perbaiki Terpilih",
            command=self._open_repair_modal,
            bg=p["emerald"],
            fg="#041510",
            activebackground=p["emerald_dark"],
            activeforeground="#FFFFFF",
            relief="flat",
            padx=14,
            pady=6,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            state="disabled",
        )
        self.repair_selected_btn.pack(side="right")

        self.diff_meta_var = tk.StringVar(value=f"Sumber resmi   {UPSTREAM_FULL_NAME}")
        tk.Label(
            ctrl,
            textvariable=self.diff_meta_var,
            font=("Consolas", 8),
            fg=p["text_muted"],
            bg=p["surface"],
        ).pack(anchor="w", pady=(6, 0))
        self.diff_next = tk.StringVar(value="Mulai dengan cakupan core untuk memeriksa file program utama.")
        tk.Label(ctrl, textvariable=self.diff_next, font=("Segoe UI", 9), fg=p["cyan"],
                 bg=p["surface"], justify="left", wraplength=710).pack(anchor="w", pady=(4, 0))

        # Summary & Filter strip
        strip = tk.Frame(page, bg=p["bg"])
        strip.pack(fill="x", pady=(0, 8))

        self.diff_counts_var = tk.StringVar(value="Zona 1: belum dipindai | Zona 2: dilindungi | Zona 3: dilewati total")
        tk.Label(strip, textvariable=self.diff_counts_var, font=("Segoe UI", 9, "bold"), fg=p["text"], bg=p["bg"]).pack(side="left")

        filter_box = tk.Frame(strip, bg=p["bg"])
        filter_box.pack(side="right")
        for fval, flabel in (
            ("ISSUES", "Bermasalah"),
            ("MISSING", "MISSING"),
            ("CORRUPTED", "CORRUPTED"),
            ("DRIFTED", "DRIFTED"),
            ("HEALTHY", "HEALTHY"),
            ("ALL", "Semua"),
        ):
            tk.Radiobutton(
                filter_box,
                text=flabel,
                variable=self._diff_filter,
                value=fval,
                command=self._populate_diff_tree,
                indicatoron=False,
                bg=p["surface_alt"],
                fg=p["text_muted"],
                selectcolor=p["cyan_soft"],
                activebackground=p["surface_hover"],
                activeforeground=p["cyan"],
                relief="flat",
                padx=8,
                pady=4,
                font=("Segoe UI", 8, "bold"),
                cursor="hand2",
            ).pack(side="left", padx=2)

        # Selection helper row
        sel_row = tk.Frame(page, bg=p["bg"])
        sel_row.pack(fill="x", pady=(0, 6))
        tk.Button(
            sel_row,
            text="Pilih semua, termasuk perubahan lokal",
            command=self._select_all_repairable,
            bg=p["surface_alt"],
            fg=p["text"],
            relief="flat",
            padx=10,
            pady=4,
            font=("Segoe UI", 8),
            cursor="hand2",
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            sel_row,
            text="Kosongkan Pilihan",
            command=self._clear_diff_selection,
            bg=p["surface_alt"],
            fg=p["text_muted"],
            relief="flat",
            padx=10,
            pady=4,
            font=("Segoe UI", 8),
            cursor="hand2",
        ).pack(side="left")
        tk.Label(
            sel_row,
            text="Klik baris untuk memilih. DRIFTED bisa berupa perubahan milikmu.",
            font=("Segoe UI", 8),
            fg=p["text_dim"],
            bg=p["bg"],
        ).pack(side="right")

        # Main Diff Table
        tbl_wrap = tk.Frame(page, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1)
        tbl_wrap.pack(fill="both", expand=True, pady=(0, 8))
        cols = ("pick", "status", "path", "reason", "size")
        self.diff_tree = ttk.Treeview(tbl_wrap, columns=cols, show="headings", style="Dark.Treeview")
        self.diff_tree.heading("pick", text="Pilih")
        self.diff_tree.heading("status", text="Status")
        self.diff_tree.heading("path", text="Path File Zona 1 (Inti MSSHI)")
        self.diff_tree.heading("reason", text="Detail")
        self.diff_tree.heading("size", text="Ukuran")
        self.diff_tree.column("pick", width=54, anchor="center")
        self.diff_tree.column("status", width=105, anchor="center")
        self.diff_tree.column("path", width=440, anchor="w")
        self.diff_tree.column("reason", width=170, anchor="w")
        self.diff_tree.column("size", width=85, anchor="e")
        vsb = ttk.Scrollbar(tbl_wrap, orient="vertical", command=self.diff_tree.yview)
        self.diff_tree.configure(yscrollcommand=vsb.set)
        self.diff_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.diff_tree.bind("<Button-1>", self._on_diff_tree_click)
        self.diff_tree.bind("<space>", self._on_diff_tree_space)

        # Zone 2 & Zone 3 Protection footer card
        z_box = tk.Frame(page, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1, padx=12, pady=8)
        z_box.pack(fill="x")
        self.zone23_var = tk.StringVar(
            value=(
                "Zona 2 (User-Owned): SOUL.md, config.yaml, .env, auth.json & custom skills tidak pernah ditimpa otomatis.  |  "
                "Zona 3 (Skip Total): 1. Private, NP, data/bin (nlm.exe), rclone, MSSHI_Recovery."
            )
        )
        tk.Label(z_box, textvariable=self.zone23_var, font=("Segoe UI", 8), fg=p["emerald"], bg=p["surface"], wraplength=860, justify="left").pack(anchor="w")

        return page

    # ------------------------------------------------------------------
    # PANEL 4: RECEIPT & ROLLBACK EXPLORER
    # ------------------------------------------------------------------
    def _build_receipts_page(self, parent: tk.Widget) -> tk.Frame:
        p = PALETTE
        page = tk.Frame(parent, bg=p["bg"], padx=18, pady=14)

        head = tk.Frame(page, bg=p["bg"])
        head.pack(fill="x", pady=(0, 10))
        tk.Label(
            head,
            text="Receipt & Rollback Explorer (msshi-runtime)",
            font=("Segoe UI", 12, "bold"),
            fg=p["text"],
            bg=p["bg"],
        ).pack(side="left")

        tk.Button(
            head,
            text="Segarkan Receipt",
            command=self.refresh_receipts,
            bg=p["surface_alt"],
            fg=p["text"],
            relief="flat",
            padx=12,
            pady=6,
            font=("Segoe UI", 9),
            cursor="hand2",
        ).pack(side="right", padx=(8, 0))

        self.rollback_btn = tk.Button(
            head,
            text="Rollback Perbaikan Ini",
            command=self._trigger_rollback,
            bg=p["amber"],
            fg="#1F1302",
            relief="flat",
            padx=14,
            pady=6,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            state="disabled",
        )
        self.rollback_btn.pack(side="right")

        split = tk.PanedWindow(page, orient="vertical", bg=p["bg"], sashwidth=6, bd=0)
        split.pack(fill="both", expand=True)

        top_box = tk.Frame(split, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1)
        cols = ("id", "category", "state", "files", "ref", "time")
        self.receipts_tree = ttk.Treeview(top_box, columns=cols, show="headings", height=10, style="Dark.Treeview")
        for col, title, width in (
            ("id", "Receipt ID", 240),
            ("category", "Kategori", 130),
            ("state", "Status", 110),
            ("files", "File", 55),
            ("ref", "Ref / Commit", 140),
            ("time", "Waktu (UTC)", 180),
        ):
            self.receipts_tree.heading(col, text=title)
            self.receipts_tree.column(col, width=width, anchor="w" if col in ("id", "time") else "center")
        vsb = ttk.Scrollbar(top_box, orient="vertical", command=self.receipts_tree.yview)
        self.receipts_tree.configure(yscrollcommand=vsb.set)
        self.receipts_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.receipts_tree.bind("<<TreeviewSelect>>", self._on_receipt_select)
        split.add(top_box, minsize=200)

        bot_box = tk.Frame(split, bg=p["surface"], highlightbackground=p["border"], highlightthickness=1, padx=10, pady=8)
        tk.Label(bot_box, text="Detail Receipt (Aman — Hanya Hash & Metadata, Tanpa Rahasia)", font=("Segoe UI", 9, "bold"), fg=p["cyan"], bg=p["surface"]).pack(anchor="w", pady=(0, 6))
        self.receipt_detail = tk.Text(
            bot_box,
            bg=p["bg"],
            fg=p["text"],
            insertbackground=p["cyan"],
            font=("Consolas", 9),
            relief="flat",
            wrap="word",
            height=12,
        )
        self.receipt_detail.pack(fill="both", expand=True)
        self.receipt_detail.configure(state="disabled")
        split.add(bot_box, minsize=180)

        return page

    # ------------------------------------------------------------------
    # PANEL 5: TROUBLESHOOTER CEPAT
    # ------------------------------------------------------------------
    def _build_troubleshooter_page(self, parent: tk.Widget) -> tk.Frame:
        p = PALETTE
        page = tk.Frame(parent, bg=p["bg"], padx=18, pady=14)

        head = tk.Frame(page, bg=p["bg"])
        head.pack(fill="x", pady=(0, 10))
        tk.Label(
            head,
            text="Troubleshooter Cepat — 5 Kendala Umum Windows & Hermes",
            font=("Segoe UI", 12, "bold"),
            fg=p["text"],
            bg=p["bg"],
        ).pack(side="left")

        tk.Button(
            head,
            text="Pindai Ulang",
            command=self.refresh_troubleshooter,
            bg=p["cyan"],
            fg="#05131F",
            relief="flat",
            padx=12,
            pady=6,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="right")

        self.ts_container = tk.Frame(page, bg=p["bg"])
        self.ts_container.pack(fill="both", expand=True)
        return page

    # ------------------------------------------------------------------
    # DATA REFRESH & BACKGROUND WORKERS
    # ------------------------------------------------------------------
    def refresh_local_dashboard(self) -> None:
        self.top_status_var.set("Memindai status lokal MSSHI & Hermes (read-only, 0 panggilan model)...")

        def on_error(exc):
            self.guide_status.set("Pemeriksaan lokal gagal. Periksa folder yang dipilih, lalu coba lagi.")
            self.guide_target.set(str(exc))
            self.top_status_var.set("Pemeriksaan lokal gagal.")
            self.guide_next.configure(state="disabled")

        self._background("local", lambda: collect_local_snapshot(
            hermes_home=self.hermes_home, msshi_home=self._launcher_home,
            target_root=self.target_root, run_cli=True,
        ), self._render_local_snapshot, on_error)

    def _render_local_snapshot(self, snap: dict) -> None:
        p = PALETTE
        self._local_snapshot = snap
        if snap.get("target_root"):
            self.target_root = Path(snap["target_root"])
        if snap.get("target_ref"):
            self.default_ref = snap["target_ref"]
            if not self._ref_var.get():
                self._ref_var.set(self.default_ref)

        has_target = bool(self.target_root and self.target_root.is_dir())
        self.guide_next.configure(state="normal" if has_target else "disabled")
        if has_target:
            self.guide_target.set(str(self.target_root))
            self.guide_status.set("Pemeriksaan lokal selesai. Bandingkan file inti untuk melihat perbedaannya."
                                  if snap.get("status") == "HEALTHY"
                                  else "Ada hal yang perlu diperiksa. Lihat hasil lokal atau bandingkan file inti.")
        else:
            self.guide_status.set("Instalasi MSSHI belum ditemukan.")
            self.guide_target.set("Jika sudah terpasang, pilih folder program MSSHI. Untuk instalasi baru, ikuti panduan pemasangan MSSHI.")
        status = str(snap.get("status") or "UNKNOWN")
        if status == "HEALTHY":
            self.overall_badge.configure(text="HEALTHY", fg=p["emerald"], bg=p["emerald_soft"])
        elif snap.get("exit_code") == 1:
            self.overall_badge.configure(text=status, fg=p["red"], bg=p["red_soft"])
        else:
            self.overall_badge.configure(text=status, fg=p["amber"], bg=p["amber_soft"])

        self.top_status_var.set(
            f"Terakhir diperiksa: {snap.get('checked_at_utc', '')[:19]}Z  •  0 model calls  •  0 rahasia dibuka"
        )
        self.hero_paths_var.set(
            f"Hermes Home : {snap.get('hermes_home')}\n"
            f"Target Root : {snap.get('target_root') or 'Belum ditemukan'} ({snap.get('target_root_source')})\n"
            f"Target Ref  : {snap.get('target_ref')}   |   Receipt tersimpan: {snap.get('receipts_count', 0)}"
        )

        hw = snap.get("hardware", {})
        disks_txt = "  ".join(
            f"{drv} bebas {info.get('free_gib')} / {info.get('total_gib')} GiB"
            for drv, info in (hw.get("disks") or {}).items()
        )
        lite_note = " • Disarankan mode hemat RAM" if hw.get("win10_4gb_lite_recommended") else ""
        self.hw_label_var.set(
            f"{hw.get('os')} ({hw.get('cpu_cores')} core) | RAM bebas {hw.get('available_ram_gib')} / {hw.get('physical_ram_gib')} GiB "
            f"(Tier {hw.get('ram_tier')}){lite_note}\nDisk: {disks_txt}"
        )
        load_pct = hw.get("memory_load_pct")
        if isinstance(load_pct, (int, float)):
            self.ram_bar["value"] = max(0, min(100, load_pct))

        # 5 Honest Status Cards
        color_map = {
            "OK": (p["emerald"], p["emerald_soft"]),
            "WARN": (p["amber"], p["amber_soft"]),
            "INFO": (p["cyan"], p["cyan_soft"]),
            "UNKNOWN": (p["text_muted"], p["surface_alt"]),
        }
        for idx, item in enumerate(snap.get("five_statuses", [])[:5]):
            title_lbl, badge_lbl, detail_lbl = self._status_card_widgets[idx]
            fg_c, bg_c = color_map.get(item.get("status", "INFO"), (p["cyan"], p["cyan_soft"]))
            title_lbl.configure(text=item.get("title", ""))
            badge_lbl.configure(text=item.get("badge", ""), fg=fg_c, bg=bg_c)
            detail_lbl.configure(text=item.get("detail", ""))

        # Profiles Tree
        for iid in self.profiles_tree.get_children():
            self.profiles_tree.delete(iid)
        for prof in snap.get("profiles", []):
            soul_st = (prof.get("soul_health") or {}).get("status", "UNKNOWN")
            itype = prof.get("interaction_type")
            itype_label = f"Tipe {itype} ({prof.get('reference_count', 0)} ref)" if itype else "-"
            readiness = prof.get("council_readiness") or prof.get("advisor_readiness") or "USER_MANAGED"
            self.profiles_tree.insert(
                "",
                "end",
                values=(
                    prof.get("profile"),
                    prof.get("role"),
                    prof.get("skill_folders", 0),
                    soul_st,
                    prof.get("trim_status"),
                    itype_label,
                    readiness,
                ),
            )

    def _prompt_local_doctor_fix(self) -> None:
        if self._working.intersection(_WRITE_JOBS | {"preview", "rollback_preview"}):
            self.top_status_var.set("Tunggu hingga tindakan yang sedang berjalan selesai.")
            return
        ok = messagebox.askyesno(
            "Konfirmasi Perbaikan Lokal",
            (
                "Jalankan perbaikan bawaan MSSHI pada instalasi yang memakai profil Hermes berikut?\n\n"
                f"{self.hermes_home}\n\n"
                "Jika hanya ingin membandingkan file program, pilih Bandingkan dengan GitHub."
            ),
            parent=self,
        )
        if not ok:
            return
        self.top_status_var.set("Menjalankan `msshi doctor --fix --json`...")

        self._background("local_fix", lambda: run_local_doctor_fix(
            msshi_home=self._launcher_home, hermes_home=self.hermes_home,
        ), self._on_local_fix_done)

    def _on_local_fix_done(self, res: dict) -> None:
        if res.get("ok"):
            messagebox.showinfo("Perbaikan Lokal Selesai", "Perbaikan lokal `msshi doctor --fix` selesai dijalankan.", parent=self)
        else:
            messagebox.showerror("Perbaikan Lokal Gagal", str(res.get("error")), parent=self)
        self.refresh_local_dashboard()

    # ------------------------------------------------------------------
    # ONLINE GITHUB DIFF ACTIONS
    # ------------------------------------------------------------------
    def run_online_check(self) -> None:
        if self.target_root is None or not self.target_root.is_dir():
            messagebox.showwarning(
                "Target Instalasi Tidak Ditemukan",
                "Direktori instalasi lokal MSSHI belum terdeteksi. Pastikan MSSHI sudah terpasang.",
                parent=self,
            )
            return
        if "diff" in self._working or "preview" in self._working:
            return
        ref = self._ref_var.get().strip() or self.default_ref
        scope = self._scope_var.get().strip() or "core"
        self.check_online_btn.configure(state="disabled", text="Memeriksa GitHub...")
        self.diff_next.set("Sedang membandingkan file. Daftar pilihan akan muncul setelah pemeriksaan selesai.")
        self.online_conn_badge.configure(text="CHECKING...", fg=PALETTE["cyan"], bg=PALETTE["cyan_soft"])

        target = self.target_root
        self._diff_report = None
        self._checked_paths.clear()
        self._populate_diff_tree()
        self._update_repair_button_state()

        def fetch():
            fallback = self.hermes_home / "msshi-upstream"
            return run_online_diff(target, ref=ref, hermes_home=self.hermes_home,
                                   scope=scope, cache_dir=self.cache_dir,
                                   local_fallback_root=fallback if fallback.is_dir() else None)

        def failed(exc):
            self.check_online_btn.configure(state="normal", text="Coba periksa lagi")
            self.online_conn_badge.configure(text="GAGAL")
            self.diff_meta_var.set(f"Perbandingan gagal. {exc}")
            self.diff_next.set("Periksa koneksi lalu coba lagi atau buka hasil pemeriksaan lokal.")

        def done(report):
            self._scanned_options = (ref, scope)
            self._render_diff_report(report)

        self._background("diff", fetch, done, failed)

    def _render_diff_report(self, report: dict) -> None:
        p = PALETTE
        self._diff_report = report
        self.check_online_btn.configure(state="normal", text="Cek ke GitHub Sekarang")

        conn = str(report.get("connection_status") or "UNKNOWN")
        if conn in ("ONLINE_OK", "ONLINE_ETAG_CACHED"):
            self.online_conn_badge.configure(text=conn, fg=p["emerald"], bg=p["emerald_soft"])
        elif "OFFLINE" in conn or "RATE_LIMITED" in conn:
            self.online_conn_badge.configure(text=conn, fg=p["amber"], bg=p["amber_soft"])
        else:
            self.online_conn_badge.configure(text=conn, fg=p["red"], bg=p["red_soft"])

        sha = (report.get("commit_sha") or "-")[:12]
        rem = report.get("rate_limit_remaining")
        err_note = f"  |  Catatan: {report.get('error_message')}" if report.get("error_message") else ""
        self.diff_meta_var.set(
            f"Ref: {report.get('github_ref')} | Commit: {sha} | Cache ETag: {report.get('from_cache')} "
            f"| Sisa Rate Limit: {rem if rem is not None else '-'}{err_note}"
        )

        s = report.get("summary", {})
        self.diff_counts_var.set(
            f"Zona 1: {s.get('total_zone1', 0)} total  •  "
            f"HEALTHY {s.get('healthy', 0)}  •  MISSING {s.get('missing', 0)}  •  "
            f"CORRUPTED {s.get('corrupted', 0)}  •  DRIFTED {s.get('drifted', 0)}"
        )
        self.zone23_var.set(
            f"Zona 2 (Dilindungi): {s.get('zone2_user_protected_count', 0)} file user terdeteksi "
            f"(peringatan SOUL: {s.get('zone2_soul_warnings', 0)}, tidak pernah ditimpa otomatis).  |  "
            f"Zona 3 (Skip Total): {s.get('zone3_strict_rules_count', 0)} jalur lokal sensitif dilewati."
        )

        # Keep potentially intentional local customizations opt-in.
        self._checked_paths = {
            row["rel_path"]
            for row in report.get("files", [])
            if row.get("status") in ("MISSING", "CORRUPTED") and row.get("repairable")
        }
        if conn not in ("ONLINE_OK", "ONLINE_ETAG_CACHED") or not report.get("commit_sha"):
            self.diff_next.set("Versi GitHub belum dapat diverifikasi. Periksa koneksi lalu coba lagi.")
        elif self._checked_paths:
            self.diff_next.set("File hilang atau rusak sudah dicentang. Periksa pilihan, lalu buka pratinjau. "
                               "File DRIFTED berisi perbedaan lokal yang perlu kamu tinjau sendiri.")
        elif s.get("drifted", 0):
            self.diff_next.set("Ada file yang berbeda dari GitHub (DRIFTED). Centang hanya jika ingin "
                               "mengganti perubahan lokal dengan salinan GitHub.")
        elif s.get("healthy", 0):
            self.diff_next.set("File yang diperiksa cocok dengan GitHub. Jika Hermes masih bermasalah, pilih Cari penyebab masalah.")
        else:
            self.diff_next.set("Belum ada file yang bisa diperbaiki. Periksa folder MSSHI dan versi sumber.")
        self._populate_diff_tree()
        self._update_repair_button_state()

    def _populate_diff_tree(self) -> None:
        for iid in self.diff_tree.get_children():
            self.diff_tree.delete(iid)
        if not self._diff_report:
            return
        flt = self._diff_filter.get()
        for row in self._diff_report.get("files", []):
            st = row.get("status")
            if flt == "ISSUES" and st == "HEALTHY":
                continue
            if flt not in ("ALL", "ISSUES") and st != flt:
                continue
            rel = row["rel_path"]
            if row.get("repairable"):
                mark = "[x]" if rel in self._checked_paths else "[ ]"
            else:
                mark = "—"
            self.diff_tree.insert(
                "",
                "end",
                iid=rel,
                values=(mark, st, rel, row.get("reason", ""), _fmt_bytes(row.get("upstream_size"))),
            )

    def _toggle_diff_row(self, iid: str) -> None:
        if not self._diff_report:
            return
        row = next((r for r in self._diff_report.get("files", []) if r["rel_path"] == iid), None)
        if not row or not row.get("repairable"):
            return
        if iid in self._checked_paths:
            self._checked_paths.remove(iid)
        else:
            self._checked_paths.add(iid)
        mark = "[x]" if iid in self._checked_paths else "[ ]"
        self.diff_tree.set(iid, "pick", mark)
        self._update_repair_button_state()

    def _on_diff_tree_click(self, event) -> None:
        iid = self.diff_tree.identify_row(event.y)
        if iid:
            self._toggle_diff_row(iid)

    def _on_diff_tree_space(self, _event) -> None:
        for iid in self.diff_tree.selection():
            self._toggle_diff_row(iid)

    def _select_all_repairable(self) -> None:
        if not self._diff_report:
            return
        self._checked_paths = {
            r["rel_path"]
            for r in self._diff_report.get("files", [])
            if r.get("status") in ("MISSING", "CORRUPTED", "DRIFTED") and r.get("repairable")
        }
        self._populate_diff_tree()
        self._update_repair_button_state()

    def _clear_diff_selection(self) -> None:
        self._checked_paths.clear()
        self._populate_diff_tree()
        self._update_repair_button_state()

    def _update_repair_button_state(self) -> None:
        can_online = bool(
            self._diff_report
            and self._scanned_options == (self._ref_var.get().strip() or self.default_ref, self._scope_var.get().strip() or "core")
            and "preview" not in self._working
            and self._diff_report.get("commit_sha")
            and self._diff_report.get("connection_status") in ("ONLINE_OK", "ONLINE_ETAG_CACHED")
        )
        n = len(self._checked_paths)
        if n > 0 and can_online:
            self.repair_selected_btn.configure(state="normal", text=f"Preview & Perbaiki ({n} File)")
        else:
            self.repair_selected_btn.configure(state="disabled", text="Preview & Perbaiki Terpilih")

    def _open_repair_modal(self) -> None:
        if str(self.repair_selected_btn.cget("state")) == "disabled" or not self._diff_report:
            return
        if self._working.intersection(_WRITE_JOBS | {"rollback_preview"}):
            self.top_status_var.set("Tunggu hingga tindakan yang sedang berjalan selesai.")
            return
        selected = sorted(self._checked_paths)
        target = self.target_root
        ref = self._diff_report["commit_sha"]
        scope = self._diff_report["scope"]
        self.repair_selected_btn.configure(text="Menyiapkan pratinjau…", state="disabled")

        def ready(plan):
            self._update_repair_button_state()
            if (self._scanned_options != (self._ref_var.get().strip() or self.default_ref, self._scope_var.get().strip() or "core")
                    or self._checked_paths != set(selected)):
                messagebox.showinfo("Pilihan berubah", "Periksa pilihan terbaru sebelum membuka pratinjau lagi.", parent=self)
                return
            if not plan["can_apply"]:
                messagebox.showinfo("Pratinjau", "File sudah sesuai atau GitHub belum terjangkau. Ulangi perbandingan.", parent=self)
                return
            if plan["commit_sha"] != ref:
                messagebox.showerror("Versi sumber berubah", "Ulangi perbandingan sebelum melanjutkan.", parent=self)
                return
            SafeRepairConfirmModal(
                self, plan, target_root=target, hermes_home=self.hermes_home,
                msshi_home=self._launcher_home, selected_files=[r["rel_path"] for r in plan["planned_files"]],
                ref=ref, scope=scope, cache_dir=self.cache_dir, on_success=self._on_online_repair_finished,
            )

        def failed(exc):
            self._update_repair_button_state()
            messagebox.showerror("Pratinjau gagal", str(exc), parent=self)

        self._background("preview", lambda: plan_online_repair(
            target, self.hermes_home, ref=ref, scope=scope, selected_files=selected, cache_dir=self.cache_dir,
        ), ready, failed)

    def _on_online_repair_finished(self, res: dict) -> None:
        self._latest_repair_id = res.get("receipt_id")
        self._show_page("receipts")
        self.refresh_local_dashboard()
        self._diff_report = None
        self._checked_paths.clear()
        self._populate_diff_tree()
        self._update_repair_button_state()

    # ------------------------------------------------------------------
    # RECEIPT & ROLLBACK EXPLORER
    # ------------------------------------------------------------------
    def refresh_receipts(self) -> None:
        self._background("receipts", lambda: discover_receipts(self.hermes_home), self._render_receipts)

    def _render_receipts(self, items: list[dict]) -> None:
        self._receipts_list = items
        self._selected_receipt = None
        self.rollback_btn.configure(state="disabled")
        for iid in self.receipts_tree.get_children():
            self.receipts_tree.delete(iid)
        for idx, rec in enumerate(items):
            ref_info = rec.get("github_ref") or "-"
            if rec.get("commit_sha"):
                ref_info = f"{ref_info} ({str(rec['commit_sha'])[:7]})"
            self.receipts_tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    rec.get("receipt_id"),
                    rec.get("category"),
                    rec.get("state"),
                    rec.get("file_count", 0),
                    ref_info,
                    str(rec.get("timestamp_utc", ""))[:19],
                ),
            )

            if self._latest_repair_id and rec["receipt_id"] == self._latest_repair_id:
                self.receipts_tree.selection_set(str(idx))
                self.receipts_tree.see(str(idx))
                self._latest_repair_id = None

    def _on_receipt_select(self, _event) -> None:
        sel = self.receipts_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._receipts_list):
            return
        rec = self._receipts_list[idx]
        self._selected_receipt = rec
        self.rollback_btn.configure(state="normal" if rec.get("can_one_click_rollback") else "disabled")

        text = ""
        try:
            rpath = reject_links(Path(rec["path"]))
            raw = json.loads(rpath.read_text(encoding="utf-8-sig"))
            text = json.dumps(raw, ensure_ascii=False, indent=2)
        except Exception as exc:
            text = f"Gagal membaca detail receipt: {exc}"

        self.receipt_detail.configure(state="normal")
        self.receipt_detail.delete("1.0", "end")
        self.receipt_detail.insert("1.0", text)
        self.receipt_detail.configure(state="disabled")

    def _trigger_rollback(self) -> None:
        rec = self._selected_receipt
        if not rec or not rec.get("can_one_click_rollback"):
            return
        if self._working.intersection(_WRITE_JOBS | {"preview", "rollback_preview"}):
            self.top_status_var.set("Tunggu hingga tindakan yang sedang berjalan selesai.")
            return
        self._background("rollback_preview", lambda: rollback_online_repair(self.hermes_home, rec["path"], apply=False),
                         lambda preview: self._confirm_rollback(rec, preview),
                         lambda exc: messagebox.showerror("Pratinjau pemulihan gagal", str(exc), parent=self))

    def _confirm_rollback(self, rec: dict, preview: dict) -> None:
        if self._selected_receipt != rec or self._active_page != "receipts":
            return
        msg = (
            f"Bukti perbaikan   {preview.get('receipt_id')}\n\n"
            f"File yang akan dipulihkan dari cadangan   {len(preview.get('restored_files', []))}\n"
            f"File baru dari perbaikan yang akan dihapus   {len(preview.get('removed_created_files', []))}\n"
            f"File yang akan dilewati karena konflik   {len(preview.get('conflicts', []))}\n\n"
            "Kembalikan file sesuai pratinjau ini?"
        )
        if not messagebox.askyesno("Konfirmasi pemulihan", msg, parent=self):
            return
        self._background("rollback", lambda: rollback_online_repair(self.hermes_home, rec["path"], apply=True),
                         self._on_rollback_done,
                         lambda exc: messagebox.showerror("Pemulihan gagal", str(exc), parent=self))

    def _on_rollback_done(self, res: dict) -> None:
        conflicts = len(res.get("conflicts", []))
        show = messagebox.showwarning if conflicts else messagebox.showinfo
        show("Hasil pemulihan",
             f"File dipulihkan   {len(res.get('restored_files', []))}\n"
             f"File dihapus   {len(res.get('removed_created_files', []))}\n"
             f"Konflik yang perlu diperiksa   {conflicts}", parent=self)
        self._diff_report = None
        self._checked_paths.clear()
        self._populate_diff_tree()
        self._update_repair_button_state()
        self.refresh_receipts()
        self.refresh_local_dashboard()

    # ------------------------------------------------------------------
    # TROUBLESHOOTER CEPAT
    # ------------------------------------------------------------------
    def refresh_troubleshooter(self) -> None:
        self._background("troubleshooter", lambda: run_all_troubleshooter_checks(
            hermes_home=self.hermes_home, msshi_home=self._launcher_home,
        ), self._render_troubleshooter)

    def _render_troubleshooter(self, report: dict) -> None:
        p = PALETTE
        for child in self.ts_container.winfo_children():
            child.destroy()

        badge_colors = {
            "OK": (p["emerald"], p["emerald_soft"]),
            "WARN": (p["amber"], p["amber_soft"]),
            "INFO": (p["cyan"], p["cyan_soft"]),
            "READY_ACTION": (p["cyan"], p["cyan_soft"]),
        }
        for chk in report.get("checks", []):
            card = tk.Frame(
                self.ts_container,
                bg=p["surface"],
                highlightbackground=p["border"],
                highlightthickness=1,
                padx=14,
                pady=10,
            )
            card.pack(fill="x", pady=(0, 8))

            row_top = tk.Frame(card, bg=p["surface"])
            row_top.pack(fill="x")
            tk.Label(row_top, text=chk["title"], font=("Segoe UI", 10, "bold"), fg=p["text"], bg=p["surface"]).pack(side="left")
            fg_c, bg_c = badge_colors.get(chk["status"], (p["cyan"], p["cyan_soft"]))
            tk.Label(row_top, text=chk["status"], font=("Segoe UI", 8, "bold"), fg=fg_c, bg=bg_c, padx=8, pady=2).pack(side="right")

            tk.Label(
                card,
                text=chk["summary"],
                font=("Segoe UI", 9),
                fg=p["text_muted"],
                bg=p["surface"],
                wraplength=820,
                justify="left",
            ).pack(anchor="w", pady=(4, 6))

            row_bot = tk.Frame(card, bg=p["surface"])
            row_bot.pack(fill="x")
            cmd_str = chk.get("recommended_command", "")
            tk.Label(row_bot, text=cmd_str, font=("Consolas", 8), fg=p["cyan"], bg=p["bg"], padx=8, pady=4).pack(side="left")

            tk.Button(
                row_bot,
                text="Salin Perintah",
                command=lambda c=cmd_str: self._copy_command(c),
                bg=p["surface_alt"],
                fg=p["text"],
                relief="flat",
                padx=10,
                pady=4,
                font=("Segoe UI", 8),
                cursor="hand2",
            ).pack(side="right", padx=(6, 0))

            if chk.get("action_id") == "launch_software_rendering":
                tk.Button(
                    row_bot,
                    text=chk.get("action_label", "Buka Hermes"),
                    command=self._launch_sw_rendering,
                    bg=p["emerald"],
                    fg="#041510",
                    relief="flat",
                    padx=12,
                    pady=4,
                    font=("Segoe UI", 8, "bold"),
                    cursor="hand2",
                ).pack(side="right")

    def _copy_command(self, cmd: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(cmd)
        self.top_status_var.set(f"Perintah disalin ke clipboard: {cmd}")

    def _launch_sw_rendering(self) -> None:
        if not messagebox.askyesno(
            "Buka Hermes (Software Rendering)",
            "Luncurkan Hermes Desktop dengan flag `--software-rendering` (HERMES_DESKTOP_DISABLE_GPU=1)?",
            parent=self,
        ):
            return
        res = launch_hermes_software_rendering(hermes_home=self.hermes_home, msshi_home=self._launcher_home)
        if res.get("ok"):
            self.top_status_var.set("Hermes Desktop diluncurkan dengan mode software rendering.")
        else:
            messagebox.showerror("Gagal Meluncurkan Hermes", str(res.get("error")), parent=self)

    def _open_saweria(self) -> None:
        try:
            webbrowser.open(SAWERIA_URL)
            self.top_status_var.set("Membuka halaman dukungan Saweria di browser.")
        except Exception:
            self._copy_command(SAWERIA_URL)

    def _check_app_self_update(self) -> None:
        self.top_status_var.set("Memeriksa rilis terbaru MSSHI Doctor di GitHub...")

        def work():
            return check_app_update(cache_dir=self.cache_dir)

        def done(report: dict) -> None:
            status = report.get("connection_status", "")
            if report.get("update_available"):
                self.top_status_var.set(f"Pembaruan Doctor tersedia: v{report.get('latest_version')}")
                summary = (
                    f"Versi terpasang: v{report.get('current_version')}\n"
                    f"Versi terbaru  : v{report.get('latest_version')} ({report.get('latest_tag')})\n"
                    f"Repositori     : {report.get('app_repo')}\n\n"
                    f"Ringkasan Changelog:\n{(report.get('changelog') or 'Lihat halaman rilis GitHub.')[:700]}\n\n"
                    "Buka halaman rilis resmi GitHub untuk melihat detail & mengunduh?"
                )
                if messagebox.askyesno("Pembaruan MSSHI Doctor Tersedia", summary, parent=self):
                    webbrowser.open(str(report.get("release_url") or SAWERIA_URL))
            elif status in ("ONLINE_OK", "ONLINE_ETAG_CACHED", "NO_RELEASES_YET"):
                self.top_status_var.set(f"MSSHI Doctor v{report.get('current_version')} sudah versi terbaru.")
                messagebox.showinfo(
                    "MSSHI Doctor Sudah Terbaru",
                    f"Anda sudah memakai MSSHI Doctor v{report.get('current_version')}.\n"
                    f"Repositori aplikasi: {report.get('app_repo')}\n\n"
                    f"Dukung pengembangan: {SAWERIA_URL}",
                    parent=self,
                )
            else:
                self.top_status_var.set("Gagal memeriksa pembaruan Doctor (offline / batas API).")
                messagebox.showwarning(
                    "Pemeriksaan Update Belum Terverifikasi",
                    f"Status: {status}\n{report.get('error_message') or 'Periksa koneksi internet lalu coba lagi.'}",
                    parent=self,
                )

        def fail(exc: Exception) -> None:
            self.top_status_var.set("Gagal memeriksa pembaruan Doctor.")
            messagebox.showwarning("Gagal Cek Update", str(exc), parent=self)

        self._background("app_self_update", work, done, fail)


def launch_gui(
    *,
    hermes_home: str | Path | None = None,
    msshi_home: str | Path | None = None,
    target_root: str | Path | None = None,
    ref: str | None = None,
    auto_close_ms: int | None = None,
) -> int:
    _enable_windows_dpi_awareness()
    app = MSSSIDoctorGUI(
        hermes_home=hermes_home,
        msshi_home=msshi_home,
        target_root=target_root,
        ref=ref,
        auto_close_ms=auto_close_ms,
    )
    app.mainloop()
    return 0
