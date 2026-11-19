"""Shared constants, security allowlists, zone policies, and UI palette."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys

APP_NAME = "MSSHI Doctor"
APP_VERSION = "1.0.0"

UPSTREAM_OWNER = "zidanaisar624"
UPSTREAM_REPO = "MSSHI-Mega-Semar-Sang-Hyang-Ismaya"
UPSTREAM_FULL_NAME = f"{UPSTREAM_OWNER}/{UPSTREAM_REPO}"
DEFAULT_REF = "v3.1.2"

APP_REPO_URL = "https://github.com/zidanaisar624/MSSHI-DOCTOR"
UPSTREAM_REPO_URL = f"https://github.com/{UPSTREAM_FULL_NAME}"

ALLOWED_GITHUB_HOSTS = frozenset({
    "api.github.com",
    "raw.githubusercontent.com",
})

# Official MSSHI release layout (aligned with scripts/msshi_cli.py in v3.1.2)
CORE_ZONE1_DIRS = (
    "scripts",
    "hermes_plugins",
    "configs",
    "skills",
    "optional",
    "templates",
)

FULL_ZONE1_DIRS = (
    *CORE_ZONE1_DIRS,
    "docs",
    "portable",
    "assets",
    "adapters",
    "business-sage",
    ".claude-plugin",
    "notebooks",
)

ZONE1_ROOT_FILES = frozenset({
    "VERSION.json",
    "CHANGELOG.md",
    "README.md",
    "README_EN.md",
    "LICENSE",
    "SKILL.md",
    "install.ps1",
    "install.sh",
    "package.json",
    "THIRD_PARTY_NOTICES.md",
    "CREDITS.md",
    "UPDATE_OTOMATIS_KE_V3.bat",
    "MULAI_DI_SINI_PEMULA.bat",
    "MULAI_SKRIPSI_DRAG_AND_DROP.bat",
    "INSTALL_FOR_AGENTS.md",
})

# Zone 2: User-owned & sensitive configuration files (never auto-overwritten)
ZONE2_FILENAMES = frozenset({
    "soul.md",
    "config.yaml",
    "config.json",
    "profile.yaml",
    "ui-preferences.json",
    "user_creative_dna.json",
    ".env",
    "auth.json",
    "auth_keys.json",
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "active_profile",
})

ZONE2_EXTENSIONS = frozenset({
    ".pem",
    ".key",
    ".p12",
    ".pfx",
})

# Zone 3: Strict local protected paths (SKIP TOTAL / DO NOT TOUCH)
ZONE3_PROTECTED_SEGMENTS = (
    "1. private",
    "2. data diri atau perkembangan diri (np)",
    "msshi_recovery",
    "msshi-private",
)

ZONE3_PROTECTED_SUBPATHS = (
    ("data", "bin"),
    (".config", "rclone"),
    ("roaming", "rclone"),
)

ZONE3_PROTECTED_FILENAMES = frozenset({
    "nlm.exe",
    "notebooklm-mcp.exe",
    "rclone.conf",
})

TEXT_EXTENSIONS = frozenset({
    ".py", ".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".ini",
    ".ps1", ".sh", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts",
    ".html", ".css", ".svg", ".xml", ".csv", ".ipynb", ".cfg",
})

BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".whl",
    ".exe", ".dll", ".pyd", ".so", ".dylib",
    ".ttf", ".otf", ".woff", ".woff2", ".mp4", ".mp3", ".wav",
})

MAX_SINGLE_DOWNLOAD_BYTES = 24 * 1024 * 1024  # 24 MiB safety cap per file
MAX_TREE_RESPONSE_BYTES = 16 * 1024 * 1024    # 16 MiB safety cap for GitHub tree JSON
HEX_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,120}$")
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Consistent exit codes aligned with msshi doctor
EXIT_HEALTHY = 0
EXIT_CORE_ERROR = 1
EXIT_NEEDS_ATTENTION = 2

# Modern-Minimalist Dark Obsidian-Slate Palette (aligned with official banner & icon)
PALETTE = {
    "bg": "#0B1117",
    "sidebar": "#0D151E",
    "surface": "#111923",
    "surface_alt": "#16202C",
    "surface_hover": "#1C2938",
    "border": "#1E293B",
    "border_strong": "#2A3A4F",
    "text": "#E6EDF3",
    "text_muted": "#8B949E",
    "text_dim": "#64748B",
    "emerald": "#10B981",
    "emerald_dark": "#059669",
    "emerald_soft": "#0D2B24",
    "cyan": "#38BDF8",
    "cyan_dark": "#0284C7",
    "cyan_soft": "#0E2538",
    "amber": "#F59E0B",
    "amber_soft": "#2E210B",
    "red": "#EF4444",
    "red_soft": "#321318",
    "violet": "#8B5CF6",
    "violet_soft": "#21193B",
}


def resolve_assets_dir() -> Path:
    """Locate the bundled assets/ directory cleanly (source tree, installed wheel, or PyInstaller)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "assets"
        if cand.is_dir():
            return cand
    pkg_dir = Path(__file__).resolve().parent
    for base in (pkg_dir.parent.parent, pkg_dir.parent, pkg_dir, Path.cwd()):
        cand = base / "assets"
        if cand.is_dir() and (cand / "msshi-doctor-icon.png").is_file():
            return cand
    return pkg_dir.parent.parent / "assets"


def resolve_cache_dir(explicit: str | Path | None = None) -> Path:
    """Resolve user-agnostic ETag cache directory outside HermesData to keep read-only probes pure."""
    if explicit:
        return Path(explicit).expanduser().absolute()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
        return (base / "MSSHI-Doctor" / "cache").absolute()
    xdg = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return (xdg / "msshi-doctor").absolute()
