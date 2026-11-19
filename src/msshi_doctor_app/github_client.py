"""Lightweight, ETag-cached GitHub REST API & raw download client (zero full-clone)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from .constants import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_REF,
    MAX_SINGLE_DOWNLOAD_BYTES,
    MAX_TREE_RESPONSE_BYTES,
    UPSTREAM_FULL_NAME,
)
from .safety import (
    SecurityViolationError,
    atomic_write_json,
    git_blob_sha1,
    normalize_rel_path,
    reject_links,
    validate_commit_sha,
    validate_git_ref,
    validate_remote_url,
)


@dataclass
class TreeSnapshot:
    status: str  # "ONLINE_OK", "ONLINE_ETAG_CACHED", "OFFLINE", "RATE_LIMITED", "ERROR"
    repo: str
    ref: str
    commit_sha: str | None
    entries: dict[str, dict]  # rel_path -> {"sha": ..., "size": ...}
    etag: str | None = None
    from_cache: bool = False
    rate_limit_remaining: int | None = None
    rate_limit_reset_utc: str | None = None
    error_message: str | None = None
    fallback_available: bool = False


class GitHubClient:
    """Zero-clone GitHub tree & selective raw file fetcher with ETag caching."""

    def __init__(
        self,
        *,
        repo: str = UPSTREAM_FULL_NAME,
        cache_dir: Path | None = None,
        api_base: str = "https://api.github.com",
        raw_base: str = "https://raw.githubusercontent.com",
        timeout: float = 15.0,
        allow_loopback_hosts: frozenset[str] | None = None,
    ) -> None:
        if repo != UPSTREAM_FULL_NAME and not allow_loopback_hosts:
            raise SecurityViolationError(f"Only official upstream repo is allowed: {UPSTREAM_FULL_NAME}")
        self.repo = repo
        self.api_base = api_base.rstrip("/")
        self.raw_base = raw_base.rstrip("/")
        self.timeout = timeout
        self.allow_loopback_hosts = allow_loopback_hosts
        self.cache_dir = cache_dir
        # Validate base URLs immediately on construction so untrusted hosts fail fast
        validate_remote_url(
            f"{self.api_base}/repos/{self.repo}/commits/{DEFAULT_REF}",
            allowed_repo=self.repo,
            allow_loopback_hosts=self.allow_loopback_hosts,
        )
        validate_remote_url(
            f"{self.raw_base}/{self.repo}/{'0' * 40}/VERSION.json",
            allowed_repo=self.repo,
            allow_loopback_hosts=self.allow_loopback_hosts,
        )

    def _headers(self, *, etag: str | None = None, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "User-Agent": f"{APP_NAME.replace(' ', '-')}/{APP_VERSION}",
            "Accept": accept,
        }
        if etag:
            headers["If-None-Match"] = etag
        # Optional read-only env var; NEVER stored, logged, or printed anywhere.
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token and not self.allow_loopback_hosts:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _cache_file(self, ref: str) -> Path | None:
        if self.cache_dir is None:
            return None
        safe_name = ref.replace("/", "_").replace("\\", "_")
        return reject_links(self.cache_dir) / f"github-tree-{safe_name}.json"

    def _load_cache(self, ref: str) -> dict | None:
        path = self._cache_file(ref)
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("repo") == self.repo and isinstance(data.get("entries"), dict):
                return data
        except (OSError, ValueError):
            return None
        return None

    def _save_cache(self, ref: str, payload: dict) -> None:
        path = self._cache_file(ref)
        if path is None:
            return
        try:
            atomic_write_json(path, payload)
        except OSError:
            pass

    @staticmethod
    def _parse_rate_limit(headers) -> tuple[int | None, str | None]:
        if not headers:
            return None, None
        rem_raw = headers.get("X-RateLimit-Remaining")
        reset_raw = headers.get("X-RateLimit-Reset")
        rem = int(rem_raw) if rem_raw and str(rem_raw).isdigit() else None
        reset_utc = None
        if reset_raw and str(reset_raw).isdigit():
            reset_utc = datetime.fromtimestamp(int(reset_raw), tz=timezone.utc).isoformat()
        return rem, reset_utc

    def _http_get_bounded(self, url: str, *, headers: dict[str, str], max_bytes: int) -> tuple[int, dict, bytes]:
        validate_remote_url(url, allowed_repo=self.repo, allow_loopback_hosts=self.allow_loopback_hosts)
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            final_url = resp.geturl()
            validate_remote_url(final_url, allowed_repo=self.repo, allow_loopback_hosts=self.allow_loopback_hosts)
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                raise ValueError(f"Remote payload exceeds safety limit ({max_bytes} bytes)")
            chunks = []
            total = 0
            while True:
                chunk = resp.read(min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"Remote payload exceeds safety limit ({max_bytes} bytes)")
            return resp.status, dict(resp.headers.items()), b"".join(chunks)

    def fetch_tree(self, ref: str = DEFAULT_REF) -> TreeSnapshot:
        """Fetch recursive Git tree for ref using ETag caching (1-2 tiny API calls, zero clone)."""
        clean_ref = validate_git_ref(ref)
        cached = self._load_cache(clean_ref)
        cached_etag = cached.get("etag") if cached else None
        commit_url = f"{self.api_base}/repos/{self.repo}/commits/{clean_ref}"

        # Step 1: Resolve ref -> exact 40-char commit SHA (with ETag!)
        commit_sha: str | None = cached.get("commit_sha") if cached else None
        commit_etag: str | None = cached.get("commit_etag") if cached else None
        rem: int | None = None
        reset_utc: str | None = None
        commit_unchanged = False

        try:
            _, resp_headers, body = self._http_get_bounded(
                commit_url,
                headers=self._headers(etag=commit_etag),
                max_bytes=1024 * 1024,
            )
            rem, reset_utc = self._parse_rate_limit(resp_headers)
            commit_etag = resp_headers.get("ETag") or resp_headers.get("etag") or commit_etag
            commit_doc = json.loads(body.decode("utf-8"))
            commit_sha = validate_commit_sha(str(commit_doc.get("sha", "")))
        except urllib.error.HTTPError as err:
            rem, reset_utc = self._parse_rate_limit(err.headers)
            if err.code == 304 and cached and commit_sha:
                commit_unchanged = True
            elif err.code in (403, 429) and (rem == 0 or err.code == 429 or "rate limit" in str(err.reason).lower()):
                return TreeSnapshot(
                    status="RATE_LIMITED",
                    repo=self.repo,
                    ref=clean_ref,
                    commit_sha=commit_sha,
                    entries=cached.get("entries", {}) if cached else {},
                    etag=cached_etag,
                    from_cache=bool(cached),
                    rate_limit_remaining=0,
                    rate_limit_reset_utc=reset_utc,
                    error_message="GitHub API rate limit tercapai. Gunakan cache/offline fallback atau atur GITHUB_TOKEN.",
                    fallback_available=True,
                )
            else:
                return TreeSnapshot(
                    status="ERROR",
                    repo=self.repo,
                    ref=clean_ref,
                    commit_sha=commit_sha,
                    entries={},
                    rate_limit_remaining=rem,
                    rate_limit_reset_utc=reset_utc,
                    error_message=f"GitHub API HTTP {err.code} untuk ref '{clean_ref}'.",
                    fallback_available=True,
                )
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            return TreeSnapshot(
                status="OFFLINE",
                repo=self.repo,
                ref=clean_ref,
                commit_sha=commit_sha,
                entries=cached.get("entries", {}) if cached else {},
                etag=cached_etag,
                from_cache=bool(cached),
                error_message=f"Koneksi ke GitHub tidak tersedia ({type(err).__name__}). Mode fallback lokal tersedia.",
                fallback_available=True,
            )

        if commit_unchanged and cached and cached.get("entries"):
            return TreeSnapshot(
                status="ONLINE_ETAG_CACHED",
                repo=self.repo,
                ref=clean_ref,
                commit_sha=commit_sha,
                entries=cached["entries"],
                etag=cached_etag,
                from_cache=True,
                rate_limit_remaining=rem,
                rate_limit_reset_utc=reset_utc,
            )

        # Step 2: Fetch recursive tree pinned to commit_sha (with ETag!)
        tree_url = f"{self.api_base}/repos/{self.repo}/git/trees/{commit_sha}?recursive=1"
        try:
            _, resp_headers, body = self._http_get_bounded(
                tree_url,
                headers=self._headers(etag=cached_etag if cached and cached.get("commit_sha") == commit_sha else None),
                max_bytes=MAX_TREE_RESPONSE_BYTES,
            )
            rem, reset_utc = self._parse_rate_limit(resp_headers)
            new_etag = resp_headers.get("ETag") or resp_headers.get("etag")
            tree_doc = json.loads(body.decode("utf-8"))
            if tree_doc.get("truncated"):
                return TreeSnapshot(
                    status="ERROR",
                    repo=self.repo,
                    ref=clean_ref,
                    commit_sha=commit_sha,
                    entries={},
                    error_message="GitHub tree response terpotong (truncated); batalkan demi keamanan.",
                    fallback_available=True,
                )
            entries: dict[str, dict] = {}
            for item in tree_doc.get("tree", []):
                if item.get("type") != "blob":
                    continue
                raw_path = item.get("path")
                blob_sha = item.get("sha")
                if not isinstance(raw_path, str) or not isinstance(blob_sha, str):
                    continue
                try:
                    clean_path = normalize_rel_path(raw_path)
                except SecurityViolationError:
                    continue
                entries[clean_path] = {
                    "sha": blob_sha,
                    "size": int(item.get("size") or 0),
                }
            self._save_cache(
                clean_ref,
                {
                    "schema_version": 1,
                    "repo": self.repo,
                    "ref": clean_ref,
                    "commit_sha": commit_sha,
                    "commit_etag": commit_etag,
                    "etag": new_etag,
                    "cached_at_utc": datetime.now(timezone.utc).isoformat(),
                    "entries": entries,
                },
            )
            return TreeSnapshot(
                status="ONLINE_OK",
                repo=self.repo,
                ref=clean_ref,
                commit_sha=commit_sha,
                entries=entries,
                etag=new_etag,
                from_cache=False,
                rate_limit_remaining=rem,
                rate_limit_reset_utc=reset_utc,
            )
        except urllib.error.HTTPError as err:
            rem, reset_utc = self._parse_rate_limit(err.headers)
            if err.code == 304 and cached and cached.get("commit_sha") == commit_sha:
                return TreeSnapshot(
                    status="ONLINE_ETAG_CACHED",
                    repo=self.repo,
                    ref=clean_ref,
                    commit_sha=commit_sha,
                    entries=cached["entries"],
                    etag=cached_etag,
                    from_cache=True,
                    rate_limit_remaining=rem,
                    rate_limit_reset_utc=reset_utc,
                )
            if err.code in (403, 429):
                return TreeSnapshot(
                    status="RATE_LIMITED",
                    repo=self.repo,
                    ref=clean_ref,
                    commit_sha=commit_sha,
                    entries=cached.get("entries", {}) if cached else {},
                    etag=cached_etag,
                    from_cache=bool(cached),
                    rate_limit_remaining=0,
                    rate_limit_reset_utc=reset_utc,
                    error_message="GitHub API rate limit tercapai saat membaca tree.",
                    fallback_available=True,
                )
            return TreeSnapshot(
                status="ERROR",
                repo=self.repo,
                ref=clean_ref,
                commit_sha=commit_sha,
                entries={},
                rate_limit_remaining=rem,
                rate_limit_reset_utc=reset_utc,
                error_message=f"GitHub Tree API HTTP {err.code}.",
                fallback_available=True,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            return TreeSnapshot(
                status="OFFLINE",
                repo=self.repo,
                ref=clean_ref,
                commit_sha=commit_sha,
                entries=cached.get("entries", {}) if cached else {},
                etag=cached_etag,
                from_cache=bool(cached),
                error_message=f"Koneksi terputus saat mengambil tree GitHub ({type(err).__name__}).",
                fallback_available=True,
            )

    def download_pinned_file(self, commit_sha: str, rel_path: str, expected_blob_sha: str) -> bytes:
        """Selectively download one file pinned to commit_sha and verify its Git blob SHA-1."""
        sha = validate_commit_sha(commit_sha)
        clean_path = normalize_rel_path(rel_path)
        url = f"{self.raw_base}/{self.repo}/{sha}/{clean_path}"
        _, _, data = self._http_get_bounded(
            url,
            headers=self._headers(accept="*/*"),
            max_bytes=MAX_SINGLE_DOWNLOAD_BYTES,
        )
        actual_blob_sha = git_blob_sha1(data)
        if actual_blob_sha != expected_blob_sha:
            raise ValueError(
                f"Checksum mismatch for {clean_path}: expected git blob {expected_blob_sha}, got {actual_blob_sha}"
            )
        return data
