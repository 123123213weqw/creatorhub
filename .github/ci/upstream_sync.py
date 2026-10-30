"""Prepare, inspect and publish an exact, regression-tested upstream merge."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

REPOSITORY = "123123213weqw/creatorhub"
UPSTREAM = "https://github.com/3441293738/creatorhub.git"
CANDIDATE_REF = "refs/heads/ci-upstream-candidate"
INITIAL_UPSTREAM = "13598884a6fcb3ccb661b6bbb7dea7c9b13fc478"
SYNC_SUBJECT = "chore: sync CreatorHub upstream "
SHA = re.compile(r"[0-9a-f]{40}\Z")


def git(repo: Path, *args: str, check: bool = True) -> str:
    command = ["git", "-c", "core.hooksPath=/dev/null", *args]
    process = subprocess.Popen(
        command, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
    )
    try:
        out, err = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise RuntimeError("Git operation timed out") from None
    if process.returncode and check:
        raise RuntimeError(err.strip() or "Git operation failed")
    if not check:
        return str(process.returncode)
    return out.strip()


def fetch(repo: Path, *args: str) -> None:
    for attempt in range(2):
        try:
            git(repo, "-c", "http.version=HTTP/1.1", "-c", "protocol.version=0",
                "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=20",
                "fetch", "--no-tags", "--no-recurse-submodules", *args)
            return
        except RuntimeError:
            if attempt:
                raise
            time.sleep(4)


def ancestor(repo: Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False) == "0"


def ensure_clean(repo: Path) -> None:
    if git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("Working tree is not clean; refusing to overwrite changes")


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    return git(repo, "diff", "--name-only", base, head).splitlines()


def reject_ci_changes(files: list[str]) -> None:
    if any(path == ".github" or path.startswith(".github/") for path in files):
        raise RuntimeError("Upstream changes .github/; manual CI review is required")


def outputs(values: dict) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(f"{key}={str(value).lower()}\n")


def summary(message: str) -> None:
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            stream.write(message + "\n")
    print(message)


def prepare(repo: Path, artifact: Path) -> dict | None:
    ensure_clean(repo)
    base = git(repo, "rev-parse", "HEAD")
    # Only this fixed public upstream may supply new source code.
    fetch(repo, UPSTREAM, "refs/heads/main")
    upstream = git(repo, "rev-parse", "FETCH_HEAD")
    previous_upstream = INITIAL_UPSTREAM
    for subject in git(repo, "log", "--first-parent", "--format=%s", base).splitlines():
        if subject.startswith(SYNC_SUBJECT) and SHA.fullmatch(subject[len(SYNC_SUBJECT):]):
            previous_upstream = subject[len(SYNC_SUBJECT):]
            break
    if not ancestor(repo, previous_upstream, upstream):
        raise RuntimeError("Upstream history was rewritten; manual review is required")
    if ancestor(repo, upstream, base):
        outputs({"changed": False, "base": base})
        summary(f"No upstream changes. Fork source remains `{base}`. No deployment.")
        return None
    common = git(repo, "merge-base", base, upstream)
    reject_ci_changes(changed_files(repo, common, upstream))
    git(repo, "config", "user.name", "github-actions[bot]")
    git(repo, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git(repo, "merge", "--no-ff", "--no-overwrite-ignore", "-m", SYNC_SUBJECT + upstream, upstream)
    ensure_clean(repo)
    candidate = git(repo, "rev-parse", "HEAD")
    reject_ci_changes(changed_files(repo, base, candidate))
    git(repo, "update-ref", CANDIDATE_REF, candidate)
    artifact.mkdir(parents=True, exist_ok=False)
    bundle = artifact / "candidate.bundle"
    git(repo, "bundle", "create", str(bundle), CANDIDATE_REF, "^" + base)
    manifest = {
        "repository": REPOSITORY, "base": base, "upstream": upstream,
        "candidate": candidate,
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "upstream_commits": int(git(repo, "rev-list", "--count", common + ".." + upstream)),
        "changed_files": changed_files(repo, base, candidate),
    }
    (artifact / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    outputs({"changed": True, **{key: manifest[key] for key in
            ("base", "upstream", "candidate", "bundle_sha256")}})
    summary(f"Prepared `{candidate}` from fork `{base}` and upstream `{upstream}`. "
            "Regression tests must pass before publication. No deployment.")
    return manifest


def import_candidate(repo: Path, artifact: Path) -> dict:
    manifest = json.loads((artifact / "manifest.json").read_text())
    if manifest.get("repository") != REPOSITORY:
        raise RuntimeError("Artifact belongs to another repository")
    for key in ("base", "upstream", "candidate"):
        if not isinstance(manifest.get(key), str) or not SHA.fullmatch(manifest[key]):
            raise RuntimeError("Invalid commit SHA in manifest")
        if manifest[key] != os.environ.get("EXPECTED_" + key.upper()):
            raise RuntimeError("Artifact does not match the prepared commit")
    expected_digest = os.environ.get("EXPECTED_BUNDLE_SHA256", "")
    digest = hashlib.sha256((artifact / "candidate.bundle").read_bytes()).hexdigest()
    if digest != manifest.get("bundle_sha256") or digest != expected_digest:
        raise RuntimeError("Candidate bundle checksum mismatch")
    ensure_clean(repo)
    if git(repo, "rev-parse", "HEAD") != manifest["base"]:
        raise RuntimeError("Checkout is not the expected base commit")
    git(repo, "bundle", "verify", str(artifact / "candidate.bundle"))
    git(repo, "fetch", "--no-tags", "--no-recurse-submodules",
        str(artifact / "candidate.bundle"), CANDIDATE_REF)
    if git(repo, "rev-parse", "FETCH_HEAD") != manifest["candidate"]:
        raise RuntimeError("Bundle reference does not match expected candidate")
    for parent in (manifest["base"], manifest["upstream"]):
        if not ancestor(repo, parent, manifest["candidate"]):
            raise RuntimeError("Candidate does not include the expected source")
    reject_ci_changes(changed_files(repo, manifest["base"], manifest["candidate"]))
    return manifest


def publish(repo: Path, artifact: Path) -> dict:
    manifest = import_candidate(repo, artifact)
    remote = git(repo, "remote", "get-url", "origin")
    if remote != f"https://github.com/{REPOSITORY}":
        if remote != f"https://github.com/{REPOSITORY}.git":
            raise RuntimeError("Refusing to publish to an unexpected repository")
    fetch(repo, "origin", "refs/heads/main")
    if git(repo, "rev-parse", "FETCH_HEAD") != manifest["base"]:
        raise RuntimeError("main changed during validation; retry on the next run")
    # Push objects only. Never check out or execute upstream code with write credentials.
    # A normal push rejects a concurrent main update; never use force or force-with-lease.
    git(repo, "push", "origin", manifest["candidate"] + ":refs/heads/main")
    summary(f"Source synchronized: `{manifest['base']}` → `{manifest['candidate']}`; "
            f"upstream `{manifest['upstream']}`. Regression job passed. No deployment.")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "checkout", "publish"))
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise RuntimeError("Workflow is restricted to the explicitly configured fork")
    repo = Path.cwd()
    if args.command == "prepare":
        prepare(repo, args.artifact.resolve())
    elif args.command == "checkout":
        manifest = import_candidate(repo, args.artifact.resolve())
        git(repo, "checkout", "--detach", manifest["candidate"])
    else:
        if os.environ.get("GITHUB_EVENT_NAME") not in {"schedule", "workflow_dispatch"}:
            raise RuntimeError("Publication is only allowed from schedule/manual runs")
        if os.environ.get("GITHUB_REF") != "refs/heads/main":
            raise RuntimeError("Publication is restricted to the main workflow")
        publish(repo, args.artifact.resolve())


if __name__ == "__main__":
    main()
