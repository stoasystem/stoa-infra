"""CDK contracts for immutable Phase 474 release storage and authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.database_stack import DatabaseStack
from stacks.frontend_stack import FrontendStack
from stacks.lambda_dist_guard import LambdaDistAsset
from stacks.notification_stack import NotificationStack
from stacks.release_delivery_stack import ReleaseDeliveryStack
from stacks.storage_stack import StorageStack


ACCOUNT = "111122223333"
REGION = "eu-central-2"
OIDC_ISSUER = "token.actions.githubusercontent.com"


def _templates() -> tuple[dict[str, Any], dict[str, Any]]:
    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    storage = StorageStack(app, "TestStorage", env=env)
    frontend = FrontendStack(app, "TestFrontend", env=env)
    delivery = ReleaseDeliveryStack(
        app,
        "TestReleaseDelivery",
        artifact_bucket=storage.release_artifact_bucket,
        evidence_bucket=storage.release_evidence_bucket,
        web_bucket=frontend.spa_bucket,
        distribution=frontend.distribution,
        env=env,
    )
    return (
        Template.from_stack(storage).to_json(),
        Template.from_stack(delivery).to_json(),
    )


def _named_resources(template: dict[str, Any], resource_type: str) -> dict[str, Any]:
    return {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    }


def _role_by_name(template: dict[str, Any], role_name: str) -> dict[str, Any]:
    roles = _named_resources(template, "AWS::IAM::Role")
    matches = [
        role
        for role in roles.values()
        if role["Properties"].get("RoleName") == role_name
    ]
    assert len(matches) == 1
    return matches[0]


def _policy_for_role(template: dict[str, Any], role_name: str) -> dict[str, Any] | None:
    role = _role_by_name(template, role_name)
    logical_id = next(
        logical_id
        for logical_id, resource in _named_resources(template, "AWS::IAM::Role").items()
        if resource is role
    )
    matches: list[dict[str, Any]] = []
    for policy in _named_resources(template, "AWS::IAM::Policy").values():
        roles = policy["Properties"].get("Roles", [])
        if {"Ref": logical_id} in roles:
            matches.append(policy)
    assert len(matches) <= 1
    return matches[0] if matches else None


def _statements(policy: dict[str, Any] | None) -> list[dict[str, Any]]:
    if policy is None:
        return []
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    return statements if isinstance(statements, list) else [statements]


def test_release_storage_is_private_versioned_encrypted_and_locked() -> None:
    storage, _ = _templates()
    buckets = _named_resources(storage, "AWS::S3::Bucket")
    release_buckets = [
        bucket
        for bucket in buckets.values()
        if bucket["Properties"].get("BucketName", "").startswith("stoa-release-")
    ]

    assert len(release_buckets) == 2
    for bucket in release_buckets:
        properties = bucket["Properties"]
        assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
        assert properties["ObjectLockEnabled"] is True
        assert properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert properties["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        assert bucket["DeletionPolicy"] == "Retain"
        assert bucket["UpdateReplacePolicy"] == "Retain"


def test_release_storage_retention_is_at_least_90_days_and_has_no_expiry() -> None:
    storage, _ = _templates()
    buckets = _named_resources(storage, "AWS::S3::Bucket")
    retention_days: dict[str, int] = {}
    for bucket in buckets.values():
        name = bucket["Properties"].get("BucketName", "")
        if not name.startswith("stoa-release-"):
            continue
        properties = bucket["Properties"]
        default_retention = properties["ObjectLockConfiguration"]["Rule"][
            "DefaultRetention"
        ]
        retention_days[name] = default_retention["Days"]
        assert default_retention["Mode"] == "GOVERNANCE"
        assert default_retention["Days"] >= 90
        assert "LifecycleConfiguration" not in properties

    assert retention_days[f"stoa-release-artifacts-{ACCOUNT}"] == 90
    assert retention_days[f"stoa-release-evidence-{ACCOUNT}"] == 2555


def test_release_roles_have_exact_github_oidc_subjects() -> None:
    _, delivery = _templates()
    expected_subjects = {
        "stoa-release-verify": "repo:stoasystem/stoa-backend:ref:refs/heads/main",
        "stoa-release-upload": "repo:stoasystem/stoa-backend:ref:refs/heads/main",
        "stoa-release-staging": "repo:stoasystem/stoa-backend:environment:staging",
        "stoa-release-production": "repo:stoasystem/stoa-backend:environment:production",
        "stoa-release-rollback": "repo:stoasystem/stoa-backend:environment:production",
    }

    for role_name, subject in expected_subjects.items():
        role = _role_by_name(delivery, role_name)
        statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert statements == [
            {
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{OIDC_ISSUER}:aud": "sts.amazonaws.com",
                        f"{OIDC_ISSUER}:sub": subject,
                    }
                },
                "Effect": "Allow",
                "Principal": {
                    "Federated": (
                        f"arn:aws:iam::{ACCOUNT}:oidc-provider/{OIDC_ISSUER}"
                    )
                },
            }
        ]


def test_verify_role_has_no_release_or_storage_authority() -> None:
    _, delivery = _templates()
    _role_by_name(delivery, "stoa-release-verify")
    assert _policy_for_role(delivery, "stoa-release-verify") is None


def test_release_role_permissions_are_separated_and_resource_scoped() -> None:
    _, delivery = _templates()
    expected_actions = {
        "stoa-release-upload": {"s3:PutObject", "s3:PutObjectTagging"},
        "stoa-release-staging": {
            "cloudfront:CreateInvalidation",
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:PutObject",
            "s3:PutObjectTagging",
        },
        "stoa-release-production": {
            "cloudfront:CreateInvalidation",
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:PutObject",
            "s3:PutObjectTagging",
        },
        "stoa-release-rollback": {
            "cloudfront:CreateInvalidation",
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:PutObject",
            "s3:PutObjectTagging",
        },
    }

    for role_name, allowed_actions in expected_actions.items():
        statements = _statements(_policy_for_role(delivery, role_name))
        assert statements
        flattened_actions: set[str] = set()
        for statement in statements:
            actions = statement["Action"]
            flattened_actions.update(actions if isinstance(actions, list) else [actions])
            resources = statement["Resource"]
            resources = resources if isinstance(resources, list) else [resources]
            assert "*" not in resources
            assert all(resource != "*" for resource in resources)
        assert flattened_actions == allowed_actions


def test_release_roles_cannot_delete_or_deploy() -> None:
    _, delivery = _templates()
    forbidden_fragments = ("Delete", "UpdateFunction", "PublishVersion", "Alias")
    for role_name in (
        "stoa-release-verify",
        "stoa-release-upload",
        "stoa-release-staging",
        "stoa-release-production",
        "stoa-release-rollback",
    ):
        for statement in _statements(_policy_for_role(delivery, role_name)):
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            assert not any(
                fragment in action
                for action in actions
                for fragment in forbidden_fragments
            )


def test_release_delivery_stack_has_no_web_pointer_resources() -> None:
    _, delivery = _templates()
    forbidden_types = {
        "AWS::DynamoDB::Table",
        "AWS::CloudFront::Distribution",
    }
    actual_types = {
        resource["Type"] for resource in delivery["Resources"].values()
    }
    assert actual_types.isdisjoint(forbidden_types)


def _api_stack(
    monkeypatch: Any, tmp_path: Path
) -> tuple[cdk.App, StorageStack, ApiStack]:
    dist_dir = tmp_path / "lambda-dist"
    dist_dir.mkdir()
    monkeypatch.setattr(
        "stacks.api_stack.verify_lambda_dist",
        lambda: LambdaDistAsset(path=dist_dir, asset_hash="0" * 64),
    )

    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    auth = AuthStack(app, "TestAuth", env=env)
    database = DatabaseStack(app, "TestDatabase", env=env)
    storage = StorageStack(app, "TestApiStorage", env=env)
    notification = NotificationStack(app, "TestNotification", env=env)
    api = ApiStack(
        app,
        "TestApi",
        user_pool=auth.user_pool,
        student_client=auth.student_client,
        parent_client=auth.parent_client,
        teacher_client=auth.teacher_client,
        admin_client=auth.admin_client,
        table=database.table,
        images_bucket=storage.images_bucket,
        reports_bucket=storage.reports_bucket,
        immutable_evidence_bucket=storage.immutable_evidence_bucket,
        teacher_queue=notification.teacher_queue,
        env=env,
    )
    return app, storage, api


def _api_template(monkeypatch: Any, tmp_path: Path) -> dict[str, Any]:
    _, _, api = _api_stack(monkeypatch, tmp_path)
    return Template.from_stack(api).to_json()


def _resource_by_property(
    template: dict[str, Any], resource_type: str, property_name: str, value: str
) -> tuple[str, dict[str, Any]]:
    matches = [
        (logical_id, resource)
        for logical_id, resource in _named_resources(template, resource_type).items()
        if resource["Properties"].get(property_name) == value
    ]
    assert len(matches) == 1
    return matches[0]


def _alias_by_function_and_name(
    template: dict[str, Any], function_name: str, alias_name: str
) -> tuple[str, dict[str, Any]]:
    function_logical_id, _ = _resource_by_property(
        template, "AWS::Lambda::Function", "FunctionName", function_name
    )
    matches = [
        (logical_id, resource)
        for logical_id, resource in _named_resources(template, "AWS::Lambda::Alias").items()
        if resource["Properties"].get("Name") == alias_name
        and function_logical_id in json.dumps(resource["Properties"]["FunctionName"])
    ]
    assert len(matches) == 1
    return matches[0]


def test_lambda_versions_and_aliases_bind_api_and_scheduler(
    monkeypatch: Any, tmp_path: Path
) -> None:
    template = _api_template(monkeypatch, tmp_path)
    aliases = _named_resources(template, "AWS::Lambda::Alias")
    assert len(aliases) == 4
    assert {resource["Properties"]["Name"] for resource in aliases.values()} == {
        "staging",
        "production",
    }

    versions = _named_resources(template, "AWS::Lambda::Version")
    assert len(versions) == 2
    for alias in aliases.values():
        assert alias["Properties"]["FunctionVersion"] != "$LATEST"
        assert "Fn::GetAtt" in alias["Properties"]["FunctionVersion"]

    api_production_alias, _ = _alias_by_function_and_name(
        template, "stoa-api", "production"
    )
    weekly_production_alias, _ = _alias_by_function_and_name(
        template, "stoa-weekly-report", "production"
    )
    integrations = _named_resources(template, "AWS::ApiGatewayV2::Integration")
    assert any(api_production_alias in json.dumps(resource) for resource in integrations.values())
    schedules = _named_resources(template, "AWS::Scheduler::Schedule")
    assert any(weekly_production_alias in json.dumps(resource) for resource in schedules.values())
    _, api_function = _resource_by_property(
        template, "AWS::Lambda::Function", "FunctionName", "stoa-api"
    )
    weekly_report_target = api_function["Properties"]["Environment"]["Variables"][
        "WEEKLY_REPORT_FUNCTION_NAME"
    ]
    assert weekly_production_alias in json.dumps(weekly_report_target)

    api_env = api_function["Properties"]["Environment"]["Variables"]
    assert api_env["STRIPE_CHECKOUT_WEB_ORIGINS"] == '["https://app.stoaedu.ch"]'
    assert api_env["APP_BASE_URL"] == "https://app.stoaedu.ch"

    github_update_policies = [
        resource
        for resource in _named_resources(template, "AWS::IAM::Policy").values()
        if resource["Properties"].get("PolicyName") == "stoa-github-backend-alias-update"
    ]
    assert len(github_update_policies) == 1
    github_statements = _statements(github_update_policies[0])
    assert len(github_statements) == 2
    function_update, alias_update = github_statements
    assert set(function_update["Action"]) == {
        "lambda:GetFunction",
        "lambda:GetFunctionConfiguration",
        "lambda:PublishVersion",
        "lambda:UpdateFunctionCode",
    }
    function_resources = json.dumps(function_update["Resource"])
    assert "StoaApiFunction" in function_resources
    assert "StoaWeeklyReportFunction" in function_resources
    assert set(alias_update["Action"]) == {
        "lambda:GetAlias",
        "lambda:GetFunction",
        "lambda:UpdateAlias",
    }


def test_release_roles_can_only_move_aliases_and_stale_dist_bypass_is_absent(
    monkeypatch: Any, tmp_path: Path
) -> None:
    app, storage, api = _api_stack(monkeypatch, tmp_path)
    frontend = FrontendStack(
        app,
        "TestAliasFrontend",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    delivery = ReleaseDeliveryStack(
        app,
        "TestAliasDelivery",
        artifact_bucket=storage.release_artifact_bucket,
        evidence_bucket=storage.release_evidence_bucket,
        web_bucket=frontend.spa_bucket,
        distribution=frontend.distribution,
        lambda_aliases=(
            api.api_staging_alias,
            api.api_production_alias,
            api.weekly_report_staging_alias,
            api.weekly_report_production_alias,
        ),
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    delivery_template = Template.from_stack(delivery).to_json()
    for role_name in (
        "stoa-release-staging",
        "stoa-release-production",
        "stoa-release-rollback",
    ):
        statements = _statements(_policy_for_role(delivery_template, role_name))
        lambda_statements = [
            statement
            for statement in statements
            if "lambda:UpdateAlias" in statement["Action"]
        ]
        assert len(lambda_statements) == 1
        assert set(lambda_statements[0]["Action"]) == {
            "lambda:GetAlias",
            "lambda:GetFunction",
            "lambda:UpdateAlias",
        }
        rendered = json.dumps(lambda_statements[0]["Resource"])
        assert "StoaApiProductionAlias" in rendered
        assert "StoaWeeklyReportProductionAlias" in rendered
        assert "UpdateFunctionCode" not in json.dumps(statements)
        assert "PublishVersion" not in json.dumps(statements)

    guard_source = Path(__file__).parents[1] / "stacks" / "lambda_dist_guard.py"
    assert "ALLOW_STALE_LAMBDA_DIST" not in guard_source.read_text(encoding="utf-8")


def test_frontend_serves_a_versioned_descriptor_and_immutable_release_prefixes() -> None:
    app = cdk.App()
    frontend = FrontendStack(
        app,
        "TestFrontend",
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    template = Template.from_stack(frontend).to_json()

    bucket = next(iter(_named_resources(template, "AWS::S3::Bucket").values()))
    assert bucket["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"
    distribution = next(
        iter(_named_resources(template, "AWS::CloudFront::Distribution").values())
    )
    behaviors = distribution["Properties"]["DistributionConfig"]["CacheBehaviors"]
    served_release = next(
        behavior for behavior in behaviors if behavior["PathPattern"] == "/served-release.json"
    )
    runtime_config = next(
        behavior for behavior in behaviors if behavior["PathPattern"] == "/runtime-config.json"
    )
    disabled_cache = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    assert served_release["CachePolicyId"] == disabled_cache
    assert runtime_config["CachePolicyId"] == disabled_cache
    assert (
        served_release["TargetOriginId"]
        == distribution["Properties"]["DistributionConfig"]["DefaultCacheBehavior"]["TargetOriginId"]
    )


def test_release_roles_can_write_only_immutable_web_prefixes_and_the_served_pointer() -> None:
    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    frontend = FrontendStack(app, "TestPointerFrontend", env=env)
    storage = StorageStack(app, "TestPointerStorage", env=env)
    delivery = ReleaseDeliveryStack(
        app,
        "TestPointerDelivery",
        artifact_bucket=storage.release_artifact_bucket,
        evidence_bucket=storage.release_evidence_bucket,
        web_bucket=frontend.spa_bucket,
        distribution=frontend.distribution,
        env=env,
    )
    template = Template.from_stack(delivery).to_json()
    for role_name in (
        "stoa-release-staging",
        "stoa-release-production",
        "stoa-release-rollback",
    ):
        rendered = json.dumps(_statements(_policy_for_role(template, role_name)))
        assert "releases/sha256/*" in rendered
        assert "served-release.json" in rendered
        assert "cloudfront:CreateInvalidation" in rendered
        assert "s3:DeleteObject" not in rendered


def test_app_passes_owned_web_resources_to_release_delivery_without_name_lookup() -> None:
    app_source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    assert "ReleaseDeliveryStack" in app_source
    assert "web_bucket=frontend.spa_bucket" in app_source
    assert "distribution=frontend.distribution" in app_source
    assert "from_bucket_name" not in app_source
    assert "manage_ses_identity=False" in app_source
    assert 'Identity.domain("stoa.ch")' not in (
        Path(__file__).parents[1] / "stacks" / "notification_stack.py"
    ).read_text(encoding="utf-8")
    assert "from_distribution_attributes" not in app_source


def test_buckets_acknowledged_by_version_id_are_versioned() -> None:
    """Report and image writes are acknowledged by version id.

    A write to an unversioned bucket returns no VersionId, so parsing the
    acknowledgement fails and the caller falls into a recovery path that lists
    versions the bucket does not keep. That is how weekly report generation
    started failing with AccessDenied on ListObjectVersions.
    """
    storage, _ = _templates()
    buckets = _named_resources(storage, "AWS::S3::Bucket")
    unversioned = [
        logical_id
        for logical_id, bucket in buckets.items()
        if logical_id.startswith(("StoaReportsBucket", "StoaImagesBucket"))
        and bucket["Properties"].get("VersioningConfiguration", {}).get("Status")
        != "Enabled"
    ]

    assert unversioned == []


def test_functions_that_invoke_a_model_may_also_count_its_tokens(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Token admission counts before invoking and fails closed.

    Granting InvokeModel without CountTokens does not degrade metering, it
    rejects every question, which is how chat came to answer 503 for everyone.
    """
    _app, _storage, api = _api_stack(monkeypatch, tmp_path)
    template = Template.from_stack(api).to_json()

    invoking = []
    for policy in _named_resources(template, "AWS::IAM::Policy").values():
        for statement in _statements(policy):
            actions = statement.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            if "bedrock:InvokeModel" in actions:
                invoking.append(actions)

    assert invoking
    for actions in invoking:
        assert "bedrock:CountTokens" in actions
        # Streaming is a distinct action, and a role holding only the buffered
        # one fails closed the moment an answer is streamed.
        assert "bedrock:InvokeModelWithResponseStream" in actions
