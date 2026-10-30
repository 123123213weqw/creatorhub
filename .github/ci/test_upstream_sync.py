"""Offline integration tests; only disposable local Git repositories are used."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import upstream_sync as sync


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "upstream"
        self.source.mkdir()
        self.raw(self.source, "init", "-b", "main")
        self.raw(self.source, "config", "user.name", "Fixture")
        self.raw(self.source, "config", "user.email", "fixture@example.invalid")
        self.commit(self.source, "app.txt", "base\n")
        self.base = self.raw(self.source, "rev-parse", "HEAD")
        self.origin = self.root / "fork.git"
        self.raw(self.root, "clone", "--bare", str(self.source), str(self.origin))
        self.worker = self.clone("prepare")
        self.artifact = self.root / "artifact"
        self.env = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": sync.REPOSITORY,
            "GITHUB_OUTPUT": str(self.root / "outputs"),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
        })
        self.env.start()
        baseline = patch.object(sync, "INITIAL_UPSTREAM", self.base)
        baseline.start()
        self.addCleanup(baseline.stop)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def raw(self, repo, *args):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo,
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def clone(self, name):
        destination = self.root / name
        self.raw(self.root, "clone", str(self.origin), str(destination))
        self.raw(destination, "config", "user.name", "Fixture")
        self.raw(destination, "config", "user.email", "fixture@example.invalid")
        return destination

    def commit(self, repo, filename, contents):
        file = repo / filename
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(contents)
        self.raw(repo, "add", filename)
        self.raw(repo, "commit", "-m", "Fixture update " + filename)

    def prepare(self):
        with patch.object(sync, "UPSTREAM", str(self.source)):
            return sync.prepare(self.worker, self.artifact)

    def candidate(self):
        self.commit(self.source, "app.txt", "upstream update\n")
        manifest = self.prepare()
        self.env_values = {"EXPECTED_" + key.upper(): manifest[key] for key in
                           ("base", "upstream", "candidate", "bundle_sha256")}
        patcher = patch.dict(os.environ, self.env_values)
        patcher.start()
        self.addCleanup(patcher.stop)
        return manifest

    def test_no_update_creates_no_artifact(self):
        self.assertIsNone(self.prepare())
        self.assertFalse(self.artifact.exists())
        self.assertIn("changed=false", (self.root / "outputs").read_text())

    def test_merge_preserves_fork_customizations(self):
        self.commit(self.worker, "integration.txt", "private integration code\n")
        manifest = self.candidate()
        self.assertTrue(sync.ancestor(self.worker, manifest["base"], manifest["candidate"]))
        self.assertTrue(sync.ancestor(self.worker, manifest["upstream"], manifest["candidate"]))
        self.assertEqual((self.worker / "integration.txt").read_text(), "private integration code\n")
        self.assertEqual((self.worker / "app.txt").read_text(), "upstream update\n")

    def test_dirty_workspace_is_not_overwritten(self):
        (self.worker / "untracked.txt").write_text("keep me")
        with self.assertRaisesRegex(RuntimeError, "not clean"):
            self.prepare()
        self.assertEqual((self.worker / "untracked.txt").read_text(), "keep me")

    def test_merge_conflict_stops_without_publishing(self):
        self.commit(self.worker, "app.txt", "fork custom change\n")
        self.commit(self.source, "app.txt", "incompatible upstream\n")
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertFalse(self.artifact.exists())
        self.assertEqual(self.raw(self.origin, "rev-parse", "main"), self.base)

    def test_upstream_ci_changes_require_review(self):
        self.commit(self.source, ".github/workflows/new.yml", "name: not trusted\n")
        with self.assertRaisesRegex(RuntimeError, "manual CI review"):
            self.prepare()
        self.assertEqual(self.raw(self.worker, "rev-parse", "HEAD"), self.base)

    def test_rewritten_upstream_history_is_rejected(self):
        self.raw(self.source, "checkout", "--orphan", "rewritten")
        self.raw(self.source, "rm", "-rf", ".")
        self.commit(self.source, "replacement.txt", "unrelated source history")
        self.raw(self.source, "branch", "-M", "main")
        with self.assertRaisesRegex(RuntimeError, "history was rewritten"):
            self.prepare()

    def test_repository_hooks_are_not_executed(self):
        marker = self.root / "hook-executed"
        hook = self.worker / ".git/hooks/post-merge"
        hook.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        hook.chmod(0o755)
        self.candidate()
        self.assertFalse(marker.exists())

    def test_exact_bundle_import_keeps_trusted_checkout(self):
        manifest = self.candidate()
        verifier = self.clone("verify")
        actual = sync.import_candidate(verifier, self.artifact)
        self.assertEqual(actual, manifest)
        self.assertEqual(self.raw(verifier, "rev-parse", "HEAD"), self.base)
        self.assertEqual((verifier / "app.txt").read_text(), "base\n")

    def test_modified_bundle_is_rejected(self):
        self.candidate()
        with open(self.artifact / "candidate.bundle", "ab") as stream:
            stream.write(b"modified")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            sync.import_candidate(self.clone("verify"), self.artifact)

    def test_artifact_identity_is_checked(self):
        self.candidate()
        path = self.artifact / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["repository"] = "another/repository"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, "another repository"):
            sync.import_candidate(self.clone("verify"), self.artifact)

    def test_candidate_must_match_prepare_output(self):
        self.candidate()
        with patch.dict(os.environ, {"EXPECTED_CANDIDATE": "a" * 40}):
            with self.assertRaisesRegex(RuntimeError, "prepared commit"):
                sync.import_candidate(self.clone("verify"), self.artifact)

    def test_wrong_checkout_is_rejected(self):
        self.candidate()
        verifier = self.clone("verify")
        self.commit(verifier, "new.txt", "concurrent local change")
        with self.assertRaisesRegex(RuntimeError, "expected base"):
            sync.import_candidate(verifier, self.artifact)

    def publishing_git(self, repo, *args, **kwargs):
        # Keep production URL enforcement while routing test I/O to a temporary bare repo.
        if args == ("remote", "get-url", "origin"):
            return "https://github.com/" + sync.REPOSITORY + ".git"
        return self.actual_git(repo, *args, **kwargs)

    def test_publish_pushes_objects_without_checking_out_candidate(self):
        manifest = self.candidate()
        writer = self.clone("writer")
        self.actual_git = sync.git
        with patch.object(sync, "git", side_effect=self.publishing_git):
            sync.publish(writer, self.artifact)
        self.assertEqual(self.raw(self.origin, "rev-parse", "main"), manifest["candidate"])
        self.assertEqual(self.raw(writer, "rev-parse", "HEAD"), self.base)
        self.assertEqual((writer / "app.txt").read_text(), "base\n")

    def test_concurrent_main_update_is_not_overwritten(self):
        self.candidate()
        writer = self.clone("writer")
        concurrent = self.clone("concurrent")
        self.commit(concurrent, "other.txt", "new independent change\n")
        self.raw(concurrent, "push", "origin", "main")
        new_main = self.raw(self.origin, "rev-parse", "main")
        self.actual_git = sync.git
        with patch.object(sync, "git", side_effect=self.publishing_git):
            with self.assertRaisesRegex(RuntimeError, "main changed"):
                sync.publish(writer, self.artifact)
        self.assertEqual(self.raw(self.origin, "rev-parse", "main"), new_main)

    def test_publish_rejects_an_unexpected_remote(self):
        self.candidate()
        with self.assertRaisesRegex(RuntimeError, "unexpected repository"):
            sync.publish(self.clone("writer"), self.artifact)


if __name__ == "__main__":
    unittest.main()
