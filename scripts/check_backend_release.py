"""Refuse to rebuild the backend unless its production release has settled.

Every infra deploy rebuilds backend main and republishes it to the five
functions. This check lowers the chance that the rebuild ships something the
backend pipeline has not shipped, or races a backend release that is running.
It is not a lock: the backend pipeline can still start after it passes.

`settled` stops when any run of the backend production workflow is unfinished,
whatever its trigger, or when the checked-out HEAD, remote main and the latest
main-push run for that commit do not agree on a successful release.

`dist` stops when the built Lambda dist was not built from that same commit, or
was built from a dirty tree.

Any API error, rate limit, short or shifting page, or missing field stops the
deploy. Nothing is retried and nothing is waved through.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.request


REPOSITORY = "stoasystem/stoa-backend"
WORKFLOW_FILE = "deploy-production.yml"
MAIN = "main"
PAGE_SIZE = 100
API_TIMEOUT_SECONDS = 30
MANIFEST_NAME = ".stoa-build-manifest.json"
FULL_SHA = re.compile(r"[0-9a-f]{40}")

Fetch = Callable[[str], dict[str, Any]]
Git = Callable[[Sequence[str]], str]


class BackendReleaseNotSettled(RuntimeError):
    """Raised when the backend release cannot be shown to be settled."""


def _github_fetch(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:  # noqa: S310
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise BackendReleaseNotSettled(f"GitHub API returned {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise BackendReleaseNotSettled(f"GitHub API could not be read: {exc}") from exc
    if not isinstance(payload, dict):
        raise BackendReleaseNotSettled("GitHub API returned something other than an object")
    return payload


def _git(args: Sequence[str]) -> str:
    try:
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BackendReleaseNotSettled(f"git {' '.join(args)} failed") from exc


def _full_sha(value: object, what: str) -> str:
    if not isinstance(value, str) or not FULL_SHA.fullmatch(value):
        raise BackendReleaseNotSettled(f"{what} is not a full commit SHA: {value!r}")
    return value


def list_workflow_runs(fetch: Fetch) -> list[dict[str, Any]]:
    """Every run of the production workflow, all pages, or an error."""
    base = (
        f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/"
        f"{WORKFLOW_FILE}/runs?per_page={PAGE_SIZE}"
    )
    runs: list[dict[str, Any]] = []
    expected_total: int | None = None
    page = 1
    while True:
        payload = fetch(f"{base}&page={page}")
        total = payload.get("total_count")
        batch = payload.get("workflow_runs")
        if not isinstance(total, int) or isinstance(total, bool) or not isinstance(batch, list):
            raise BackendReleaseNotSettled(f"page {page} of the run list is malformed")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise BackendReleaseNotSettled("the run list changed while it was being read")
        if not all(isinstance(run, dict) for run in batch):
            raise BackendReleaseNotSettled(f"page {page} holds a malformed run")
        runs.extend(batch)
        if len(runs) >= expected_total:
            break
        if len(batch) < PAGE_SIZE:
            raise BackendReleaseNotSettled(
                f"the run list ended at {len(runs)} of {expected_total} runs"
            )
        page += 1
    ids = [run.get("id") for run in runs]
    if len(runs) != expected_total or len(set(ids)) != len(ids):
        raise BackendReleaseNotSettled("the run list is incomplete or repeats a run")
    return runs


def check_settled(
    backend_root: Path,
    *,
    expected_sha: str | None = None,
    fetch: Fetch = _github_fetch,
    git: Git = _git,
) -> str:
    """Return the backend commit that is safe to rebuild, or raise."""
    local = _full_sha(
        git(["-C", str(backend_root), "rev-parse", "HEAD"]).strip(), "the backend checkout"
    )
    if expected_sha is not None and local != expected_sha:
        raise BackendReleaseNotSettled(
            f"the backend checkout is {local}, but {expected_sha} was confirmed earlier"
        )
    remote_lines = git(
        ["ls-remote", f"https://github.com/{REPOSITORY}", f"refs/heads/{MAIN}"]
    ).splitlines()
    if len(remote_lines) != 1:
        raise BackendReleaseNotSettled("remote main could not be resolved to one commit")
    remote = _full_sha(remote_lines[0].split("\t", 1)[0], "remote main")
    if local != remote:
        raise BackendReleaseNotSettled(f"the backend checkout is {local}, remote main is {remote}")

    runs = list_workflow_runs(fetch)
    unfinished = []
    for run in runs:
        if not isinstance(run.get("id"), int) or isinstance(run.get("id"), bool):
            raise BackendReleaseNotSettled("a backend production run has no id")
        status = run.get("status")
        for key in ("status", "event", "head_sha"):
            if not isinstance(run.get(key), str):
                raise BackendReleaseNotSettled(f"run {run['id']} has no {key}")
        # An explicit null is a real value; an absent key is missing evidence,
        # and treating it as null would drop the run from the search below.
        if "head_branch" not in run:
            raise BackendReleaseNotSettled(f"run {run['id']} has no head_branch")
        if not isinstance(run["head_branch"], (str, type(None))):
            raise BackendReleaseNotSettled(f"run {run['id']} has a malformed head_branch")
        if status != "completed":
            unfinished.append(f"{run.get('id')} ({status}, {run.get('event')})")
    if unfinished:
        raise BackendReleaseNotSettled(
            "backend production runs are not finished: " + ", ".join(unfinished)
        )

    for_head = [
        run
        for run in runs
        if run.get("head_sha") == local
        and run.get("head_branch") == MAIN
        and run.get("event") == "push"
    ]
    if not for_head:
        raise BackendReleaseNotSettled(f"no main push run of the backend release exists for {local}")
    # Run ids grow with creation. A re-run keeps its id, so this picks the
    # newest run for the commit, not the most recently re-run one; the live
    # alias read that follows is what catches an older release re-run over it.
    latest = max(for_head, key=lambda run: run["id"])
    if latest.get("conclusion") != "success":
        raise BackendReleaseNotSettled(
            f"the backend release run {latest['id']} for {local} concluded "
            f"{latest.get('conclusion')!r}, not 'success'"
        )
    return local


def check_dist(dist: Path, expected_sha: str) -> None:
    """Refuse a dist that was not built from the confirmed commit."""
    try:
        manifest = json.loads((dist / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackendReleaseNotSettled(f"the dist manifest could not be read: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BackendReleaseNotSettled("the dist manifest is malformed")
    if manifest.get("source_git_sha") != expected_sha:
        raise BackendReleaseNotSettled(
            f"the dist was built from {manifest.get('source_git_sha')!r}, not {expected_sha}"
        )
    if manifest.get("source_git_dirty") is not False:
        raise BackendReleaseNotSettled("the dist was built from a dirty backend tree")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    settled = commands.add_parser("settled")
    settled.add_argument("--backend-root", type=Path, required=True)
    settled.add_argument("--expected-sha", default=None)
    dist = commands.add_parser("dist")
    dist.add_argument("--dist", type=Path, required=True)
    dist.add_argument("--expected-sha", required=True)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Fetch = _github_fetch,
    git: Git = _git,
) -> int:
    args = parse_args(argv)
    try:
        expected = (
            None if args.expected_sha is None else _full_sha(args.expected_sha, "--expected-sha")
        )
        if args.command == "settled":
            sha = check_settled(args.backend_root, expected_sha=expected, fetch=fetch, git=git)
            output = os.environ.get("GITHUB_OUTPUT")
            if output:
                with open(output, "a", encoding="utf-8") as handle:
                    handle.write(f"sha={sha}\n")
            print(f"Backend release settled: sha={sha}")
        else:
            assert expected is not None
            check_dist(args.dist, expected)
            print(f"Lambda dist is the settled backend release: sha={expected}")
    except BackendReleaseNotSettled as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
