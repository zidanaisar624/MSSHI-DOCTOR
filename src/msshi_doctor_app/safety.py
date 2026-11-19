"""Security, 3-zone path protection, hashing, corruption checks, and atomic I/O."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import urlparse

from .constants import (
    ALLOWED_GITHUB_HOSTS,
    BINARY_EXTENSIONS,
    CORE_ZONE1_DIRS,
    FULL_ZONE1_DIRS,
    HEX_SHA40_RE,
    SAFE_REF_RE,
    TEXT_EXTENSIONS,
    UPSTREAM_FULL_NAME,
    ZONE1_ROOT_FILES,
    ZONE2_EXTENSIONS,
    ZONE2_FILENAMES,
    ZONE3_PROTECTED_FILENAMES,
    ZONE3_PROTECTED_SEGMENTS,
    ZONE3_PROTECTED_SUBPATHS,
)


class SecurityViolationError(ValueError):
    """Raised when an operation violates path, host, symlink, or zone boundaries."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_sha1(data: bytes) -> str:
    """Compute exact Git object blob SHA-1: sha1('blob <len>\\0' + data)."""
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & 0x400)


def reject_links(path: Path) -> Path:
    """Reject symlinks, Windows junctions, UNC paths, and NUL bytes."""
    raw = str(path)
    if "\x00" in raw or raw.startswith(("\\\\", "//")):
        raise SecurityViolationError(f"Unsafe or UNC path rejected: {raw!r}")
    resolved = Path(os.path.abspath(Path(path).expanduser()))
    for item in (resolved, *resolved.parents):
        if os.path.lexists(item) and is_reparse_or_symlink(item):
            raise SecurityViolationError(f"Symlink or junction rejected: {item}")
    return resolved


def normalize_rel_path(rel_path: str) -> str:
    """Validate and normalize a repository-relative POSIX path without traversal."""
    if not isinstance(rel_path, str) or not rel_path or "\x00" in rel_path:
        raise SecurityViolationError("Empty or invalid relative path.")
    cleaned = rel_path.replace("\\", "/")
    if cleaned.startswith("/") or cleaned.startswith("~"):
        raise SecurityViolationError(f"Absolute path rejected: {rel_path!r}")
    parts = cleaned.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise SecurityViolationError(f"Path traversal or drive specifier rejected: {rel_path!r}")
    return "/".join(parts)


def safe_join_within_root(root: Path, rel_path: str) -> Path:
    """Resolve rel_path strictly inside root, rejecting traversal and symlinks."""
    clean_root = reject_links(root)
    clean_rel = normalize_rel_path(rel_path)
    candidate = Path(os.path.abspath(clean_root.joinpath(*clean_rel.split("/"))))
    try:
        candidate.relative_to(clean_root)
    except ValueError as exc:
        raise SecurityViolationError(f"Target escapes root boundary: {rel_path!r}") from exc
    for item in (candidate, *candidate.parents):
        if os.path.lexists(item) and is_reparse_or_symlink(item):
            raise SecurityViolationError(f"Symlink or junction inside path rejected: {item}")
        if item == clean_root:
            break
    if is_zone3_protected(candidate):
        raise SecurityViolationError(f"Zone 3 strictly protected path rejected: {candidate}")
    return candidate


def validate_git_ref(ref: str) -> str:
    if not isinstance(ref, str) or not SAFE_REF_RE.fullmatch(ref) or ".." in ref or ref.startswith(("/", "-")):
        raise SecurityViolationError(f"Unsafe Git ref rejected: {ref!r}")
    return ref


def validate_commit_sha(commit_sha: str) -> str:
    if not isinstance(commit_sha, str) or not HEX_SHA40_RE.fullmatch(commit_sha):
        raise SecurityViolationError(f"Invalid 40-hex commit SHA: {commit_sha!r}")
    return commit_sha


def validate_remote_url(
    url: str,
    *,
    allowed_repo: str = UPSTREAM_FULL_NAME,
    allow_loopback_hosts: frozenset[str] | None = None,
) -> str:
    """Enforce official HTTPS GitHub host & upstream repository allowlist."""
    if not isinstance(url, str) or not url.strip():
        raise SecurityViolationError("Empty remote URL.")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if allow_loopback_hosts and host in allow_loopback_hosts:
        if parsed.scheme not in ("http", "https"):
            raise SecurityViolationError(f"Unsupported URL scheme: {parsed.scheme!r}")
        return url
    if parsed.scheme != "https":
        raise SecurityViolationError(f"Non-HTTPS remote URL rejected: {url!r}")
    if host not in ALLOWED_GITHUB_HOSTS:
        raise SecurityViolationError(f"Untrusted host rejected (only official GitHub allowed): {host!r}")
    if parsed.username or parsed.password:
        raise SecurityViolationError("Embedded credentials in URL are rejected.")
    path = parsed.path or ""
    if host == "api.github.com":
        expected_prefix = f"/repos/{allowed_repo}/"
        if not path.startswith(expected_prefix):
            raise SecurityViolationError(f"GitHub API path outside official repo rejected: {path!r}")
    elif host == "raw.githubusercontent.com":
        expected_prefix = f"/{allowed_repo}/"
        if not path.startswith(expected_prefix):
            raise SecurityViolationError(f"Raw GitHub path outside official repo rejected: {path!r}")
    return url


def is_zone3_protected(path: Path | str) -> bool:
    """Return True if path touches any Zone 3 strict local protected area."""
    raw = str(path).replace("\\", "/")
    parts = [p.lower() for p in raw.split("/") if p]
    if not parts:
        return False
    for segment in ZONE3_PROTECTED_SEGMENTS:
        if segment in parts:
            return True
    if parts[-1] in ZONE3_PROTECTED_FILENAMES:
        return True
    for sub in ZONE3_PROTECTED_SUBPATHS:
        n = len(sub)
        for i in range(len(parts) - n + 1):
            if tuple(parts[i : i + n]) == sub:
                return True
    return False


def is_zone2_user_owned(path: Path | str) -> bool:
    """Return True if path is user-owned config, SOUL, secret, or credential file."""
    raw = str(path).replace("\\", "/")
    parts = [p.lower() for p in raw.split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    if name in ZONE2_FILENAMES:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if name.startswith("mcp-tokens"):
        return True
    suffix = Path(name).suffix.lower()
    if suffix in ZONE2_EXTENSIONS:
        return True
    return False


def classify_repo_path(rel_path: str, *, scope: str = "core") -> str:
    """Classify a repo-relative path into zone1_core, zone2_user, zone3_protected, or excluded."""
    try:
        clean = normalize_rel_path(rel_path)
    except SecurityViolationError:
        return "zone3_protected"
    if is_zone3_protected(clean):
        return "zone3_protected"
    if is_zone2_user_owned(clean):
        return "zone2_user"
    parts = clean.split("/")
    if len(parts) == 1:
        return "zone1_core" if clean in ZONE1_ROOT_FILES else "excluded"
    top = parts[0]
    allowed_dirs = FULL_ZONE1_DIRS if scope == "full" else CORE_ZONE1_DIRS
    if top in allowed_dirs:
        return "zone1_core"
    return "excluded"


def is_text_path(rel_path: str) -> bool:
    name = rel_path.rsplit("/", 1)[-1]
    suffix = Path(name).suffix.lower()
    if suffix in BINARY_EXTENSIONS:
        return False
    if suffix in TEXT_EXTENSIONS:
        return True
    if name in {"LICENSE", "Dockerfile", "Makefile"} or "." not in name:
        return True
    return False


def detect_corruption(rel_path: str, data: bytes, expected_size: int | None = None) -> str | None:
    """Detect concrete local file corruption; returns reason code or None if structurally valid."""
    if expected_size is not None and expected_size > 0 and len(data) == 0:
        return "empty_file"
    if not is_text_path(rel_path):
        return None
    if b"\x00" in data:
        return "nul_byte"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "invalid_utf8"
    suffix = Path(rel_path).suffix.lower()
    if suffix == ".json":
        try:
            json.loads(text)
        except ValueError:
            return "invalid_json"
    elif suffix == ".py":
        try:
            ast.parse(text, filename=rel_path)
        except SyntaxError:
            return "invalid_python_syntax"
    return None


def matches_git_blob_sha(rel_path: str, local_bytes: bytes, expected_blob_sha: str) -> bool:
    """Check whether local file matches GitHub's Git blob SHA (handling Windows CRLF text safely)."""
    if git_blob_sha1(local_bytes) == expected_blob_sha:
        return True
    if is_text_path(rel_path) and b"\x00" not in local_bytes and b"\r\n" in local_bytes:
        normalized = local_bytes.replace(b"\r\n", b"\n")
        if git_blob_sha1(normalized) == expected_blob_sha:
            return True
    return False


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Atomically write bytes via sibling .tmp file, fsync, and os.replace."""
    target = reject_links(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".msshi-repair-", suffix=".tmp", delete=False
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None and os.name != "nt":
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, encoded)


def check_soul_health_passive(soul_path: Path) -> dict:
    """Read-only passive UTF-8 / NUL health check for SOUL.md without returning content."""
    result = {
        "exists": False,
        "valid_utf8": False,
        "nul_free": False,
        "non_empty": False,
        "size_bytes": 0,
        "sha256": None,
        "status": "MISSING",
        "auto_repair_allowed": False,
    }
    try:
        clean = reject_links(soul_path)
    except SecurityViolationError:
        result["status"] = "LINK_REJECTED"
        return result
    if not clean.is_file():
        return result
    try:
        raw = clean.read_bytes()
    except OSError:
        result["status"] = "UNREADABLE"
        return result
    result["exists"] = True
    result["size_bytes"] = len(raw)
    result["sha256"] = sha256_bytes(raw)
    result["non_empty"] = len(raw) > 0
    result["nul_free"] = b"\x00" not in raw
    try:
        raw.decode("utf-8-sig")
        result["valid_utf8"] = True
    except UnicodeDecodeError:
        result["valid_utf8"] = False
    if result["non_empty"] and result["nul_free"] and result["valid_utf8"]:
        result["status"] = "HEALTHY"
    else:
        result["status"] = "CORRUPTED_REVIEW_REQUIRED"
    return result
