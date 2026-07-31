"""Least-privilege identities for immutable release storage operations.

This stack accepts already-owned Lambda aliases, a Web bucket, and its
CloudFront distribution. It scopes release roles to immutable aliases and one
versioned served-release pointer without mutable code authority.
"""

from __future__ import annotations

from collections.abc import Sequence

from aws_cdk import (
    Stack,
    aws_cloudfront as cloudfront,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
)
from constructs import Construct


GITHUB_OIDC_ISSUER = "token.actions.githubusercontent.com"
GITHUB_REPOSITORY = "stoasystem/stoa-backend"


class ReleaseDeliveryStack(Stack):
    """Create exact-subject GitHub OIDC roles around immutable release stores."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        artifact_bucket: s3.IBucket,
        evidence_bucket: s3.IBucket,
        web_bucket: s3.IBucket,
        distribution: cloudfront.IDistribution,
        lambda_aliases: Sequence[lambda_.IAlias] = (),
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self,
            "GitHubOidcProvider",
            (
                f"arn:aws:iam::{self.account}:"
                f"oidc-provider/{GITHUB_OIDC_ISSUER}"
            ),
        )

        verify_role = self._github_role(
            "VerifyRole",
            role_name="stoa-release-verify",
            subject=f"repo:{GITHUB_REPOSITORY}:ref:refs/heads/main",
            provider=provider,
        )
        # The verification job gets an identity for auditable trust evaluation,
        # but deliberately receives no AWS resource or deployment authority.
        self.verify_role = verify_role

        self.upload_role = self._github_role(
            "UploadRole",
            role_name="stoa-release-upload",
            subject=f"repo:{GITHUB_REPOSITORY}:ref:refs/heads/main",
            provider=provider,
        )
        self._grant_object_write(
            self.upload_role,
            artifact_bucket,
            "candidates/sha256/*",
        )
        self._grant_object_write(
            self.upload_role,
            evidence_bucket,
            "verification/*",
        )

        self.staging_role = self._github_role(
            "StagingRole",
            role_name="stoa-release-staging",
            subject=f"repo:{GITHUB_REPOSITORY}:environment:staging",
            provider=provider,
        )
        self._grant_object_read(
            self.staging_role,
            artifact_bucket,
            "candidates/sha256/*",
        )
        self._grant_object_write(
            self.staging_role,
            evidence_bucket,
            "staging/*",
        )
        self._grant_alias_transition(self.staging_role, lambda_aliases)
        self._grant_web_release_transition(
            self.staging_role,
            web_bucket,
            distribution,
        )

        self.production_role = self._github_role(
            "ProductionRole",
            role_name="stoa-release-production",
            subject=f"repo:{GITHUB_REPOSITORY}:environment:production",
            provider=provider,
        )
        self._grant_object_read(
            self.production_role,
            artifact_bucket,
            "candidates/sha256/*",
        )
        self._grant_object_write(
            self.production_role,
            evidence_bucket,
            "production/*",
        )
        self._grant_alias_transition(self.production_role, lambda_aliases)
        self._grant_web_release_transition(
            self.production_role,
            web_bucket,
            distribution,
        )

        self.rollback_role = self._github_role(
            "RollbackRole",
            role_name="stoa-release-rollback",
            subject=f"repo:{GITHUB_REPOSITORY}:environment:production",
            provider=provider,
        )
        self._grant_object_read(
            self.rollback_role,
            artifact_bucket,
            "candidates/sha256/*",
        )
        self._grant_object_write(
            self.rollback_role,
            evidence_bucket,
            "rollback/*",
        )
        self._grant_alias_transition(self.rollback_role, lambda_aliases)
        self._grant_web_release_transition(
            self.rollback_role,
            web_bucket,
            distribution,
        )

    def _github_role(
        self,
        construct_id: str,
        *,
        role_name: str,
        subject: str,
        provider: iam.IOpenIdConnectProvider,
    ) -> iam.Role:
        principal = iam.OpenIdConnectPrincipal(
            provider,
            conditions={
                "StringEquals": {
                    f"{GITHUB_OIDC_ISSUER}:aud": "sts.amazonaws.com",
                    f"{GITHUB_OIDC_ISSUER}:sub": subject,
                }
            },
        )
        return iam.Role(
            self,
            construct_id,
            role_name=role_name,
            assumed_by=principal,
            description="STOA immutable release pipeline role",
        )

    @staticmethod
    def _grant_object_read(
        role: iam.Role,
        bucket: s3.IBucket,
        key_pattern: str,
    ) -> None:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[bucket.arn_for_objects(key_pattern)],
            )
        )

    @staticmethod
    def _grant_object_write(
        role: iam.Role,
        bucket: s3.IBucket,
        key_pattern: str,
    ) -> None:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:PutObjectTagging"],
                resources=[bucket.arn_for_objects(key_pattern)],
            )
        )

    @staticmethod
    def _grant_alias_transition(
        role: iam.Role,
        aliases: Sequence[lambda_.IAlias],
    ) -> None:
        """Allow only alias reads/updates; callers enforce SHA and revision guards."""
        if not aliases:
            return
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:GetAlias", "lambda:GetFunction", "lambda:UpdateAlias"],
                resources=[alias.function_arn for alias in aliases],
            )
        )

    @staticmethod
    def _grant_web_release_transition(
        role: iam.Role,
        web_bucket: s3.IBucket,
        distribution: cloudfront.IDistribution,
    ) -> None:
        """Permit exact object-version readback and bounded pointer transitions."""
        immutable_web_objects = web_bucket.arn_for_objects("releases/sha256/*")
        served_descriptor = web_bucket.arn_for_objects("served-release.json")
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"],
                resources=[immutable_web_objects, served_descriptor],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudfront:CreateInvalidation"],
                resources=[distribution.distribution_arn],
            )
        )
