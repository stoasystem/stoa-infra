"""Unit tests for Lambda environment merge. These must not call AWS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stacks.lambda_environment import (
    checkout_origins_for,
    load_live_lambda_environment,
    merge_lambda_environment,
)


def test_production_checkout_origin_is_https_app_host() -> None:
    assert checkout_origins_for("production") == '["https://app.stoaedu.ch"]'


def test_merge_keeps_live_audit_keys_that_cdk_does_not_declare() -> None:
    merged = merge_lambda_environment(
        {"ENVIRONMENT": "production", "STRIPE_CHECKOUT_WEB_ORIGINS": '["https://app.stoaedu.ch"]'},
        {
            "AUTHORIZATION_AUDIT_ACTIVE_KEY": "hex:ab",
            "AUTHORIZATION_AUDIT_ACTIVE_KEY_ID": "prod-v1",
            "STRIPE_CHECKOUT_WEB_ORIGINS": '["http://localhost:5173"]',
        },
        env_name="production",
    )
    assert merged["AUTHORIZATION_AUDIT_ACTIVE_KEY"] == "hex:ab"
    assert merged["STRIPE_CHECKOUT_WEB_ORIGINS"] == '["https://app.stoaedu.ch"]'


def test_merge_refuses_a_production_snapshot_that_would_drop_audit_material() -> None:
    with pytest.raises(RuntimeError, match="AUTHORIZATION_AUDIT_ACTIVE_KEY"):
        merge_lambda_environment(
            {"ENVIRONMENT": "production", "STRIPE_CHECKOUT_WEB_ORIGINS": '["https://app.stoaedu.ch"]'},
            {"STRIPE_CHECKOUT_WEB_ORIGINS": '["https://app.stoaedu.ch"]'},
            env_name="production",
        )


def test_load_live_environment_fails_closed_when_required_and_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STOA_REQUIRE_LIVE_LAMBDA_ENV", "1")
    monkeypatch.delenv("STOA_LIVE_LAMBDA_ENV_FILE", raising=False)
    with pytest.raises(RuntimeError, match="STOA_LIVE_LAMBDA_ENV_FILE"):
        load_live_lambda_environment("stoa-api", env_name="production")

    snapshot = tmp_path / "env.json"
    snapshot.write_text(json.dumps({"stoa-weekly-report": {"ENVIRONMENT": "production"}}), encoding="utf-8")
    monkeypatch.setenv("STOA_LIVE_LAMBDA_ENV_FILE", str(snapshot))
    with pytest.raises(RuntimeError, match="stoa-api"):
        load_live_lambda_environment("stoa-api", env_name="production")


def test_load_live_environment_returns_the_named_function_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = tmp_path / "env.json"
    snapshot.write_text(
        json.dumps({"stoa-api": {"ENVIRONMENT": "production", "K": "v"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("STOA_LIVE_LAMBDA_ENV_FILE", str(snapshot))
    monkeypatch.setenv("STOA_REQUIRE_LIVE_LAMBDA_ENV", "1")
    assert load_live_lambda_environment("stoa-api", env_name="production") == {
        "ENVIRONMENT": "production",
        "K": "v",
    }
    assert load_live_lambda_environment("stoa-sandbox-api", env_name="sandbox") is None


def test_snapshot_script_writes_function_maps_without_printing_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util

    script = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_lambda_environment.py"
    spec = importlib.util.spec_from_file_location("snapshot_lambda_environment", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    payloads = {
        "stoa-api": {"AUTHORIZATION_AUDIT_ACTIVE_KEY": "hex:secret", "ENVIRONMENT": "production"},
        "stoa-weekly-report": {"ENVIRONMENT": "production"},
        "stoa-dispatch-reconciler": {"ENVIRONMENT": "production"},
    }

    def fake_run(argv, text=True):
        name = argv[argv.index("--function-name") + 1]
        return json.dumps(payloads[name])

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    github_env = tmp_path / "github.env"
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    monkeypatch.setattr(module.subprocess, "check_output", fake_run)

    assert module.main() == 0
    written = json.loads((tmp_path / "live-lambda-env.json").read_text(encoding="utf-8"))
    assert written["stoa-api"]["AUTHORIZATION_AUDIT_ACTIVE_KEY"] == "hex:secret"
    logged = capsys.readouterr().out
    assert "hex:secret" not in logged
    assert "stoa-api: 2 keys" in logged
    assert "STOA_REQUIRE_LIVE_LAMBDA_ENV=1" in github_env.read_text(encoding="utf-8")


def test_a_function_not_deployed_yet_is_recorded_rather_than_fatal(tmp_path, monkeypatch):
    """CDK cannot preserve an environment that does not exist yet."""
    import importlib.util
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "snapshot_lambda_environment.py"
    spec = importlib.util.spec_from_file_location("snapshot_new_function", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_run(argv, text=True):
        name = argv[argv.index("--function-name") + 1]
        if name == "stoa-dispatch-reconciler":
            raise subprocess.CalledProcessError(254, argv)
        return json.dumps({"ENVIRONMENT": "production"})

    monkeypatch.setattr(module.subprocess, "check_output", fake_run)

    snapshot = module.snapshot_lambda_environments()

    assert snapshot["stoa-dispatch-reconciler"] is None
    assert snapshot["stoa-api"] == {"ENVIRONMENT": "production"}


def test_a_name_nobody_queried_is_still_refused(tmp_path, monkeypatch):
    from stacks.lambda_environment import load_live_lambda_environment

    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"stoa-api": {"ENVIRONMENT": "production"}}), encoding="utf-8")
    monkeypatch.setenv("STOA_LIVE_LAMBDA_ENV_FILE", str(path))
    monkeypatch.setenv("STOA_REQUIRE_LIVE_LAMBDA_ENV", "1")

    with pytest.raises(RuntimeError, match="missing stoa-dispatch-reconciler"):
        load_live_lambda_environment("stoa-dispatch-reconciler", env_name="production")


def test_a_function_recorded_as_not_deployed_synths_without_live_keys(tmp_path, monkeypatch):
    from stacks.lambda_environment import load_live_lambda_environment

    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"stoa-dispatch-reconciler": None}), encoding="utf-8")
    monkeypatch.setenv("STOA_LIVE_LAMBDA_ENV_FILE", str(path))
    monkeypatch.setenv("STOA_REQUIRE_LIVE_LAMBDA_ENV", "1")

    assert load_live_lambda_environment("stoa-dispatch-reconciler", env_name="production") is None
