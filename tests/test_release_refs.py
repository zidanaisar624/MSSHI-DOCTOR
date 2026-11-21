"""A branch name, commit SHA and cached tree must always identify the same release."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from msshi_doctor_app.constants import UPSTREAM_FULL_NAME
from msshi_doctor_app.github_client import GitHubClient
from msshi_doctor_app.safety import git_blob_sha1


class ReleaseRefTests(unittest.TestCase):
    def setUp(self):
        fixture = TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.client = GitHubClient(cache_dir=Path(fixture.name))
        self.old_sha = "a" * 40
        self.new_sha = "b" * 40
        self.cache = dict(repo=UPSTREAM_FULL_NAME, ref="main", commit_sha=self.old_sha,
                          entries={"README.md": dict(sha=self.old_sha, size=20)}, etag='"old"')

    def test_cache_must_identify_exact_ref_and_contain_valid_entries(self):
        self.client._save_cache("main", self.cache)
        self.assertEqual(self.client._load_cache("main"), self.cache)
        self.assertNotEqual(self.client._cache_file("release/1"), self.client._cache_file("release_1"))
        for invalid in (dict(ref="old"), dict(commit_sha="bad"), dict(entries={"bad": {"sha": "?"}})):
            self.client._save_cache("main", {**self.cache, **invalid})
            self.assertIsNone(self.client._load_cache("main"))

    def test_branch_moves_then_loses_connection_before_new_tree(self):
        self.client._save_cache("main", self.cache)
        with patch.object(self.client, "_http_get_bounded", side_effect=[
            (200, {}, self.new_sha.encode()), URLError("offline"),
        ]):
            tree = self.client.fetch_tree("main")
        self.assertEqual(tree.commit_sha, self.new_sha)
        self.assertEqual(tree.entries, {})
        self.assertFalse(tree.from_cache)
        self.assertEqual(tree.status, "OFFLINE")

    def test_pinned_ref_mismatch_is_refused_before_downloading(self):
        with patch.object(self.client, "_http_get_bounded", return_value=(200, {}, self.new_sha.encode())) as get:
            with self.assertRaisesRegex(ValueError, "SHA"):
                self.client.fetch_tree(self.old_sha)
        self.assertEqual(get.call_count, 1)

    def test_branch_name_raw_filenames_and_tree_modes(self):
        docs = {"tree": [
            {"type": "blob", "mode": mode, "path": name, "size": 2, "sha": git_blob_sha1(b"ok")}
            for mode, name in (("100644", "docs/a #1.md"), ("120000", "scripts/link"))
        ]}
        with patch.object(self.client, "_http_get_bounded", side_effect=[
            (200, {}, self.new_sha.encode()), (200, {}, json.dumps(docs).encode()), (200, {}, b"ok"),
        ]) as get:
            tree = self.client.fetch_tree("release/stable")
            self.assertEqual(list(tree.entries), ["docs/a #1.md"])
            self.assertTrue(get.call_args_list[0].args[0].endswith("release%2Fstable"))
            self.assertEqual(self.client.download_pinned_file(self.new_sha, "docs/a #1.md", git_blob_sha1(b"ok")), b"ok")
            self.assertTrue(get.call_args.args[0].endswith("/docs/a%20%231.md"))
            self.assertNotIn("Authorization", get.call_args.kwargs["headers"])
