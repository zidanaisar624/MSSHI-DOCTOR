<p align="center">
  <img src="assets/msshi-doctor-banner.jpg" alt="MSSHI Doctor — Online Diagnostics, GitHub Diff & Safe Repair" width="100%" />
</p>

<h1 align="center">
  <img src="assets/msshi-doctor-icon-64.png" alt="MSSHI Doctor Icon" width="32" align="absmiddle" />
  &nbsp;MSSHI Doctor App
</h1>

<p align="center">
  <strong>Aplikasi Diagnostik Lokal, Online GitHub Diff, Safe Repair 6-Langkah, dan Rollback 1-Klik untuk Ekosistem MSSHI &amp; Hermes Agent.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-38BDF8?style=flat-square" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/Dependencies-Zero%20Mandatory-10B981?style=flat-square" alt="Zero Dependencies" />
  <img src="https://img.shields.io/badge/RAM%20Target-%3C%2080%20MB%20(Win10%204GB)-10B981?style=flat-square" alt="Win10 4GB Friendly" />
  <img src="https://img.shields.io/badge/Paid%20Model%20Calls-0-10B981?style=flat-square" alt="Zero Paid Calls" />
  <img src="https://img.shields.io/badge/License-MIT-64748B?style=flat-square" alt="MIT License" />
</p>

---

## Ringkasan

**MSSHI Doctor App** adalah aplikasi desktop dan CLI mandiri yang dirancang khusus untuk memeriksa kesehatan instalasi lokal **[MSSHI (Mega Semar Sang Hyang Ismaya)](https://github.com/zidanaisar624/MSSHI-Mega-Semar-Sang-Hyang-Ismaya)** dan **Hermes Agent** secara jujur, ringan, dan aman:

- **Zero Full-Clone Online GitHub Diff:** Membandingkan file inti lokal terhadap rilis resmi GitHub (`v3.1.2` atau target ref pilihan Anda) via GitHub REST API + `ETag` (`If-None-Match`) caching tanpa perlu `git clone` penuh dan tanpa perlu login GitHub untuk repo publik.
- **Model Perlindungan 3-Zona Ketat:** Memisahkan file inti MSSHI (**Zona 1**), file milik pengguna seperti `SOUL.md`, `config.yaml`, `.env`, `auth.json` (**Zona 2 — tidak pernah ditimpa otomatis**), dan jalur sensitif lokal (**Zona 3 — dilewati total**).
- **Safe Repair 6-Langkah Atomik & 1-Click Rollback:** Dry-Run Preview &rarr; Atomic Backup &rarr; Selective Download terpin ke `commit_sha` 40-hex &rarr; Verifikasi Git Blob SHA-1 &amp; penulisan `.tmp` atomik &rarr; Receipt JSON &rarr; Re-check otomatis `msshi doctor --json` + Rollback 1-klik dengan deteksi konflik.
- **5 Status Jujur:** Membedakan secara tegas antara *Instruksi Ditemukan*, *Rute Terdaftar*, *Runtime Tersedia*, *Akun &amp; Konfigurasi*, dan *Uji Eksekusi Nyata (`0` kredit / `0` panggilan model berbayar)*.
- **Ramah Laptop Windows 10 RAM 4 GB &amp; Portabel Drive C:/D:** Dibangun dengan **Python 3.10+ stdlib (`tkinter`/`ttk`)** tanpa Electron/Chromium (konsumsi RAM GUI sekitar 30–50 MB), tanpa dependensi wajib pihak ketiga, serta mendeteksi lokasi instalasi secara adaptif (`HERMES_HOME`, `MSSHI_HOME`, Drive `D:` maupun laptop Drive `C:` saja).

---

## Pemisahan 2 Repositori

- **Repo Aplikasi Ini:** `https://github.com/zidanaisar624/MSSHI-DOCTOR` — berisi kode sumber aplikasi GUI &amp; CLI, aset branding resmi, test suite, dan konfigurasi build.
- **Repo Referensi Upstream:** `https://github.com/zidanaisar624/MSSHI-Mega-Semar-Sang-Hyang-Ismaya` — dibaca secara read-only sebagai sumber kebenaran saat pengecekan online diff dan pengunduhan selektif file inti Zona 1.

---

## Cara Menjalankan

### 1. Mode Desktop GUI (1-Klik di Windows)
Klik dua kali [`MSSHI_Doctor.bat`](MSSHI_Doctor.bat), atau jalankan dari terminal:

```powershell
python -m msshi_doctor_app --gui
```

### 2. Mode CLI (Otomatisasi &amp; JSON)
Anda dapat langsung menggunakan wrapper [`msshi-doctor.cmd`](msshi-doctor.cmd) atau menginstal paket lokal secara editable (`pip install -e .`):

```powershell
# Diagnostik lokal read-only (5 status jujur, hardware, 6 profil native)
.\msshi-doctor.cmd --local --json

# Perbandingan online ke GitHub resmi (zero full-clone + ETag cache + fallback offline)
.\msshi-doctor.cmd --online --json --ref v3.1.2

# Preview rencana perbaikan online (0 file diubah)
.\msshi-doctor.cmd --online-fix --dry-run --json

# Terapkan perbaikan selektif 6-langkah dengan backup & receipt
.\msshi-doctor.cmd --online-fix --apply --files VERSION.json scripts/msshi_cli.py --json

# Daftar semua receipt & backup lokal
.\msshi-doctor.cmd --receipts --json

# Preview & terapkan 1-Click Rollback dari receipt online-repair
.\msshi-doctor.cmd --rollback <RECEIPT_ID> --dry-run --json
.\msshi-doctor.cmd --rollback <RECEIPT_ID> --apply --json

# Troubleshooter cepat (Grey screen, Skills timeout, Gateway, Lightcraft, PATH)
.\msshi-doctor.cmd --troubleshoot --json
```

### Kode Keluar (Exit Code) Konsisten
- `0` — `HEALTHY` (semua pemeriksaan inti lulus).
- `1` — `CORE_ERROR` (file inti rusak/korup, pelanggaran keamanan, atau target tidak ditemukan).
- `2` — `NEEDS_ATTENTION` (ada file `MISSING`/`DRIFTED`, peringatan `SOUL.md`, atau status `OFFLINE`/`RATE_LIMITED`).

---

## Arsitektur Keamanan 3-Zona

1. **Zona 1 — MSSHI-Owned Core Files (Boleh Diperbaiki Setelah Preview):**
   Direktori inti `scripts/`, `hermes_plugins/`, `configs/`, `skills/`, `optional/`, `templates/` serta file root resmi seperti `VERSION.json` dan `README.md`.
   Status file: `HEALTHY`, `MISSING`, `CORRUPTED` (NUL byte, UTF-8 rusak, 0 byte tak terduga, JSON/Python syntax rusak), dan `DRIFTED`.
2. **Zona 2 — User-Owned &amp; Custom Files (Dilarang Ditimpa Otomatis):**
   `SOUL.md`, `config.yaml`, `.env`, `auth.json`, key/sertifikat, pilihan model/provider/endpoint, dan custom skill milik pengguna. Nilai rahasia **tidak pernah dibaca, disimpan, atau dicetak**.
3. **Zona 3 — Strict Local Protected Paths (Dilewati Total):**
   Folder pribadi (`1. Private`, `2. Data Diri atau Perkembangan Diri (NP)`), `data/bin/` (`nlm.exe`, `notebooklm-mcp.exe`), konfigurasi `rclone` (`gdrive:`), dan `MSSHI_Recovery/`.

### Pengamanan Tambahan
- **HTTPS &amp; Host Allowlist Ketat:** Hanya mengizinkan `https://api.github.com/repos/zidanaisar624/MSSHI-Mega-Semar-Sang-Hyang-Ismaya/...` dan `https://raw.githubusercontent.com/zidanaisar624/MSSHI-Mega-Semar-Sang-Hyang-Ismaya/<40-hex-commit-sha>/...`.
- **Anti-Traversal &amp; Anti-Link:** Menolak path `../`, drive escape, UNC path, symlink, dan Windows junction.
- **Opsional `GITHUB_TOKEN`:** Jika variabel lingkungan `GITHUB_TOKEN` tersedia, klien menggunakannya di memori untuk memperbesar kuota API tanpa pernah menyimpan atau mencetak token tersebut.

---

## 5 Panel GUI Utama

1. **Dashboard Kesehatan:** Hero banner, 5 Status Jujur, telemetri RAM/CPU/Disk (indikator mode hemat RAM), tabel kesehatan 6 profil native, serta tombol perbaikan lokal `msshi doctor --fix` dengan konfirmasi.
2. **Online GitHub Sync &amp; Diff:** Pemilih ref (`v3.1.2`, `main`) &amp; cakupan (`core`/`full`), indikator status `ONLINE_OK` / `ONLINE_ETAG_CACHED` / `OFFLINE` / `RATE_LIMITED`, filter status, checklist pemilihan per-file, dan ringkasan perlindungan Zona 2 &amp; 3.
3. **Modal Konfirmasi Perbaikan Aman:** Menampilkan commit terpin, lokasi backup atomik, daftar file dan ukuran unduhan, jaminan perlindungan Zona 2/3, lalu menjalankan 6 langkah secara transaksional.
4. **Receipt &amp; Rollback Explorer:** Menjelajahi receipt di `msshi-runtime` (hanya hash &amp; metadata aman) serta tombol **Rollback Perbaikan Ini** dengan preview konflik.
5. **Troubleshooter Cepat:** Pemeriksaan real-time untuk layar Hermes abu-abu (dengan peluncur `--software-rendering`), folder `skills/` berat, ringkasan log gateway, kunci proses Lightcraft, dan cek PATH `msshi`.

---

## Struktur Proyek &amp; Pengujian

```text
MSSHI-DOCTOR/
├── assets/                          # Banner 16:9 resmi & icon aplikasi (.png + multi-res .ico)
├── build/msshi-doctor.spec          # Spesifikasi build PyInstaller Windows (.exe)
├── docs/reports/                    # Laporan implementasi & verifikasi (.md & .pdf)
├── src/msshi_doctor_app/
│   ├── constants.py                 # Palet tema, allowlist keamanan, & kebijakan 3-zona
│   ├── safety.py                    # Hash Git blob/SHA-256, anti-traversal, deteksi korup, atomic I/O
│   ├── github_client.py             # Klien GitHub zero-clone + ETag cache + selective download
│   ├── local_probe.py               # Penemuan adaptif C:/D:, jembatan CLI msshi, 5 status jujur
│   ├── diff_engine.py               # Mesin perbandingan 3-kategori + fallback offline
│   ├── repair_engine.py             # Mesin Safe Repair 6-langkah + 1-Click Rollback
│   ├── troubleshooter.py            # 5 pemeriksaan kendala umum Windows & Hermes
│   ├── cli.py                       # Antarmuka CLI (--local, --online, --online-fix, --rollback)
│   └── gui.py                       # Antarmuka Desktop modern-minimalis (Tkinter/ttk)
├── tests/                           # Automated test suite (offline + mock GitHub HTTP server)
├── MSSHI_Doctor.bat                 # Launcher GUI 1-klik Windows
├── msshi-doctor.cmd                 # Launcher CLI Windows
└── pyproject.toml                   # Konfigurasi paket PEP 621
```

### Menjalankan Test Suite
```powershell
$env:PYTHONPATH = "src"
python -B -m unittest discover -s tests -v
```

### Build Executable Mandiri (Opsional)
```powershell
pip install pyinstaller
pyinstaller build/msshi-doctor.spec
```

---

## Lisensi

Dirilis di bawah lisensi [MIT](LICENSE).
