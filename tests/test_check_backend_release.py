"""Decisions of the backend-release check, against stand-ins for GitHub and git.

These prove what the script does with what it reads. They do not prove what the
GitHub API or a runner's token actually return; only a real run shows that.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_backend_release.py"
spec = importlib.util.spec_from_file_location("check_backend_release", SCRIPT)
assert spec and spec.loader
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

HEAD = "a" * 40
OLDER = "b" * 40
ROOT = Path("/runner/stoa-backend")


def _run(run_id: int, *, sha: str = HEAD, status: str = "completed",
         conclusion: str | None = "success", event: str = "push",
         branch: str | None = "main") -> dict[str, Any]:
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "head_sha": sha,
        "head_branch": branch,
    }


class FakeGitHub:
    def __init__(self, runs: list[dict[str, Any]], *, page_size: int = check.PAGE_SIZE) -> None:
        self.runs = runs
        self.page_size = page_size
        self.pages: list[int] = []
        self.total_override: dict[int, Any] = {}
        self.page_override: dict[int, Any] = {}

    def fetch(self, url: str) -> dict[str, Any]:
        query = parse_qs(urlparse(url).query)
        assert "/actions/workflows/deploy-production.yml/runs" in url
        assert query["per_page"] == [str(check.PAGE_SIZE)]
        # No event or branch filter: an unfinished run of any trigger counts.
        assert "event" not in query and "branch" not in query and "status" not in query
        page = int(query["page"][0])
        self.pages.append(page)
        if page in self.page_override:
            return self.page_override[page]
        start = (page - 1) * self.page_size
        return {
            "total_count": self.total_override.get(page, len(self.runs)),
            "workflow_runs": self.runs[start:start + self.page_size],
        }


def _git(local: str = HEAD, remote: str = HEAD):
    def git(args: list[str]) -> str:
        if args[-2:] == ["rev-parse", "HEAD"]:
            assert args[:2] == ["-C", str(ROOT)]
            return local + "\n"
        if args[0] == "ls-remote":
            assert args[1:] == ["https://github.com/stoasystem/stoa-backend", "refs/heads/main"]
            return f"{remote}\trefs/heads/main\n"
        raise AssertionError(args)

    return git


def _settled(github: FakeGitHub, *, git=None, expected_sha: str | None = None) -> str:
    return check.check_settled(
        ROOT, expected_sha=expected_sha, fetch=github.fetch, git=git or _git()
    )


def _refused(github: FakeGitHub, match: str, **kwargs: Any) -> None:
    with pytest.raises(check.BackendReleaseNotSettled, match=match):
        _settled(github, **kwargs)


def test_a_settled_release_of_head_is_confirmed() -> None:
    github = FakeGitHub([_run(3), _run(2, sha=OLDER), _run(1, sha=OLDER, conclusion="failure")])
    assert _settled(github) == HEAD


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "pending"])
def test_any_unfinished_run_stops_the_deploy(status: str) -> None:
    _refused(FakeGitHub([_run(3), _run(2, sha=OLDER, status=status, conclusion=None)]),
             "not finished")


def test_an_unfinished_run_of_another_trigger_still_stops_the_deploy() -> None:
    github = FakeGitHub(
        [_run(3), _run(2, status="in_progress", conclusion=None, event="workflow_dispatch")]
    )
    _refused(github, "workflow_dispatch")


def test_an_unfinished_run_on_a_later_page_stops_the_deploy() -> None:
    runs = [_run(300 - i, sha=OLDER) for i in range(250)]
    runs[0] = _run(300)
    runs[230] = _run(70, sha=OLDER, status="in_progress", conclusion=None)
    github = FakeGitHub(runs)
    _refused(github, "not finished")
    assert github.pages == [1, 2, 3]


def test_every_page_is_read_however_many_there_are() -> None:
    runs = [_run(1000)] + [_run(999 - i, sha=OLDER) for i in range(420)]
    github = FakeGitHub(runs)
    assert _settled(github) == HEAD
    assert github.pages == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped", None])
def test_a_head_release_that_did_not_succeed_stops_the_deploy(conclusion: str | None) -> None:
    _refused(FakeGitHub([_run(3, conclusion=conclusion), _run(2, sha=OLDER)]), "not 'success'")


def test_the_newest_run_for_head_decides_even_if_an_older_one_succeeded() -> None:
    _refused(FakeGitHub([_run(5, conclusion="failure"), _run(4)]), "run 5")


def test_an_older_success_is_not_evidence_for_head() -> None:
    _refused(FakeGitHub([_run(2, sha=OLDER)]), "no main push run")


@pytest.mark.parametrize(
    "run",
    [
        _run(3, event="workflow_dispatch"),
        _run(3, branch="feature"),
        _run(3, branch=None),
    ],
)
def test_only_a_main_push_run_is_evidence_for_head(run: dict[str, Any]) -> None:
    _refused(FakeGitHub([run]), "no main push run")


def test_an_empty_run_list_stops_the_deploy() -> None:
    _refused(FakeGitHub([]), "no main push run")


def test_a_checkout_behind_remote_main_stops_the_deploy() -> None:
    _refused(FakeGitHub([_run(3)]), "remote main is", git=_git(remote=OLDER))


def test_a_checkout_other_than_the_one_confirmed_first_stops_the_recheck() -> None:
    _refused(FakeGitHub([_run(3)]), "confirmed earlier", expected_sha=OLDER)


def test_a_recheck_of_the_same_checkout_passes() -> None:
    assert _settled(FakeGitHub([_run(3)]), expected_sha=HEAD) == HEAD


def test_the_run_list_changing_between_pages_stops_the_deploy() -> None:
    github = FakeGitHub([_run(300)] + [_run(299 - i, sha=OLDER) for i in range(150)])
    github.total_override = {2: 152}
    _refused(github, "changed while it was being read")


def test_a_short_page_before_the_total_stops_the_deploy() -> None:
    github = FakeGitHub([_run(300)] + [_run(299 - i, sha=OLDER) for i in range(150)])
    github.page_override = {2: {"total_count": 151, "workflow_runs": [_run(1, sha=OLDER)]}}
    _refused(github, "ended at 101 of 151")


def test_a_repeated_run_across_pages_stops_the_deploy() -> None:
    runs = [_run(300)] + [_run(299 - i, sha=OLDER) for i in range(99)] + [_run(300)]
    _refused(FakeGitHub(runs), "repeats a run")


@pytest.mark.parametrize(
    "payload",
    [
        {"workflow_runs": []},
        {"total_count": "1", "workflow_runs": []},
        {"total_count": True, "workflow_runs": []},
        {"total_count": 1, "workflow_runs": {}},
        {"total_count": 1, "workflow_runs": ["x"]},
    ],
)
def test_a_malformed_page_stops_the_deploy(payload: dict[str, Any]) -> None:
    github = FakeGitHub([])
    github.page_override = {1: payload}
    _refused(github, "malformed")


@pytest.mark.parametrize("missing", ["id", "status", "event", "head_sha"])
def test_a_run_missing_a_field_stops_the_deploy(missing: str) -> None:
    run = _run(3)
    del run[missing]
    _refused(FakeGitHub([run]), "has no")


def test_a_newest_run_missing_its_branch_does_not_fall_back_to_an_older_success() -> None:
    newest = _run(5, conclusion="failure")
    del newest["head_branch"]
    _refused(FakeGitHub([newest, _run(4)]), "run 5 has no head_branch")


def test_a_malformed_branch_stops_the_deploy() -> None:
    _refused(FakeGitHub([_run(3, branch=7)]), "malformed head_branch")


def test_an_api_error_stops_the_deploy() -> None:
    def fetch(url: str) -> dict[str, Any]:
        raise check.BackendReleaseNotSettled("GitHub API returned 403 for " + url)

    with pytest.raises(check.BackendReleaseNotSettled, match="403"):
        check.check_settled(ROOT, fetch=fetch, git=_git())


def test_an_http_error_from_github_is_reported_not_raised_raw(monkeypatch) -> None:
    import urllib.error

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "rate limited", {}, None)

    monkeypatch.setattr(check.urllib.request, "urlopen", urlopen)
    with pytest.raises(check.BackendReleaseNotSettled, match="429"):
        check._github_fetch("https://api.github.com/x")


def test_a_short_sha_from_git_stops_the_deploy() -> None:
    _refused(FakeGitHub([_run(3)]), "not a full commit SHA", git=_git(local="abc123"))


def _dist(tmp_path: Path, manifest: object) -> Path:
    (tmp_path / check.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_a_dist_of_the_confirmed_commit_passes(tmp_path: Path) -> None:
    check.check_dist(_dist(tmp_path, {"source_git_sha": HEAD, "source_git_dirty": False}), HEAD)


@pytest.mark.parametrize(
    ("manifest", "match"),
    [
        ({"source_git_sha": OLDER, "source_git_dirty": False}, "built from"),
        ({"source_git_sha": HEAD, "source_git_dirty": True}, "dirty"),
        ({"source_git_sha": HEAD}, "dirty"),
        ([], "malformed"),
    ],
)
def test_a_dist_that_is_not_the_confirmed_commit_stops_the_deploy(
    tmp_path: Path, manifest: object, match: str
) -> None:
    with pytest.raises(check.BackendReleaseNotSettled, match=match):
        check.check_dist(_dist(tmp_path, manifest), HEAD)


def test_a_missing_dist_manifest_stops_the_deploy(tmp_path: Path) -> None:
    with pytest.raises(check.BackendReleaseNotSettled, match="could not be read"):
        check.check_dist(tmp_path, HEAD)


def test_main_writes_the_confirmed_sha_for_later_steps(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    status = check.main(
        ["settled", "--backend-root", str(ROOT)],
        fetch=FakeGitHub([_run(3)]).fetch,
        git=_git(),
    )
    assert status == 0
    assert output.read_text(encoding="utf-8") == f"sha={HEAD}\n"


def test_main_fails_and_writes_no_sha_when_not_settled(tmp_path: Path, monkeypatch, capsys) -> None:
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    status = check.main(
        ["settled", "--backend-root", str(ROOT)],
        fetch=FakeGitHub([_run(3, status="queued", conclusion=None)]).fetch,
        git=_git(),
    )
    assert status == 1
    assert not output.exists()
    assert "::error::" in capsys.readouterr().err


def test_main_refuses_a_malformed_expected_sha(capsys) -> None:
    status = check.main(["dist", "--dist", "/nowhere", "--expected-sha", "HEAD"])
    assert status == 1


# Where the checks sit in the deploy workflow. Read as text: the infra venv has
# no YAML parser, and the order of named steps is what matters here.
WORKFLOW = (SCRIPT.parents[1] / ".github" / "workflows" / "deploy-production.yml").read_text(
    encoding="utf-8"
)


def _step(name: str) -> str:
    marker = f"      - name: {name}\n"
    assert WORKFLOW.count(marker) == 1, name
    start = WORKFLOW.index(marker)
    end = WORKFLOW.find("\n      - name: ", start + len(marker))
    return WORKFLOW[start:] if end == -1 else WORKFLOW[start:end]


def _at(name: str) -> int:
    return WORKFLOW.index(f"      - name: {name}\n")


def test_the_settled_check_runs_before_the_backend_is_built_or_credentials_exist() -> None:
    settled = _at("Check the backend release has settled")
    assert _at("Set up Python") < settled < _at("Build Lambda dist (CDK hashes it as an asset)")
    assert settled < _at("Configure AWS credentials (OIDC)")
    step = _step("Check the backend release has settled")
    assert "id: backend_release" in step
    assert "GITHUB_TOKEN: ${{ github.token }}" in step
    assert "check_backend_release.py settled --backend-root stoa-backend" in step


def test_the_dist_is_bound_to_the_settled_commit_after_it_is_built() -> None:
    at = _at("Check the dist is the settled backend release")
    assert _at("Verify Lambda dist provenance") < at < _at("Install CDK dependencies")
    step = _step("Check the dist is the settled backend release")
    assert "CONFIRMED_SHA: ${{ steps.backend_release.outputs.sha }}" in step
    assert '--expected-sha "$CONFIRMED_SHA"' in step


def test_live_production_is_checked_once_credentials_exist_and_again_right_before_deploy() -> None:
    live = _at("Check production serves the settled backend release")
    recheck = _at("Recheck the backend release right before deploying")
    deploy = _at("Deploy production stacks")
    assert _at("Configure AWS credentials (OIDC)") < live < _at("CDK diff")
    assert _at("CDK diff") < recheck < deploy
    # Nothing but the recheck between it and the deploy.
    assert WORKFLOW.count("      - name: ", recheck, deploy) == 1
    step = _step("Recheck the backend release right before deploying")
    assert "set -euo pipefail" in step
    assert '--expected-sha "$CONFIRMED_SHA"' in step
    assert step.count("--phase pre-deploy") == 1
    assert "settled" in step


def test_the_live_release_is_verified_after_the_deploy_against_this_build() -> None:
    at = _at("Verify live production release")
    assert _at("Deploy production stacks") < at < _at("Publish change surface to run summary")
    step = _step("Verify live production release")
    assert "--phase post-deploy" in step
    assert "--local-dist stoa-backend/dist" in step


def test_no_check_is_allowed_to_fail_quietly() -> None:
    for name in (
        "Check the backend release has settled",
        "Check the dist is the settled backend release",
        "Check production serves the settled backend release",
        "Recheck the backend release right before deploying",
        "Verify live production release",
    ):
        step = _step(name)
        assert "continue-on-error" not in step
        assert "|| true" not in step
        assert "if:" not in step
        # The confirmed commit reaches the shell through env, never inlined.
        run = step.split("run:", 1)[1]
        assert "${{" not in run


def test_release_records_are_kept_whatever_the_checks_found() -> None:
    summary = _step("Publish release records to run summary")
    upload = _step("Upload release records")
    assert _at("Verify live production release") < _at("Publish release records to run summary")
    assert "if: always()" in summary
    assert "if: always()" in upload
    assert "actions/upload-artifact@" in upload
    assert "retention-days: 90" in upload
    assert "name: release-record-attempt-${{ github.run_attempt }}" in upload
