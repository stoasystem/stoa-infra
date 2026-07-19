"""CDK contracts for immutable Phase 474 release storage and authority."""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.release_delivery_stack import ReleaseDeliveryStack
from stacks.storage_stack import StorageStack


ACCOUNT = "111122223333"
REGION = "eu-central-2"
OIDC_ISSUER = "token.actions.githubusercontent.com"


def _templates() -> tuple[dict[str, Any], dict[str, Any]]:
    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    storage = StorageStack(app, "TestStorage", env=env)
    delivery = ReleaseDeliveryStack(
        app,
        "TestReleaseDelivery",
        artifact_bucket=storage.release_artifact_bucket,
        evidence_bucket=storage.release_evidence_bucket,
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
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:PutObject",
            "s3:PutObjectTagging",
        },
        "stoa-release-production": {
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:PutObject",
            "s3:PutObjectTagging",
        },
        "stoa-release-rollback": {
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


def test_plan_does_not_create_aliases_or_web_pointer_resources() -> None:
    _, delivery = _templates()
    forbidden_types = {
        "AWS::Lambda::Alias",
        "AWS::Lambda::Version",
        "AWS::DynamoDB::Table",
        "AWS::CloudFront::Distribution",
    }
    actual_types = {
        resource["Type"] for resource in delivery["Resources"].values()
    }
    assert actual_types.isdisjoint(forbidden_types)
