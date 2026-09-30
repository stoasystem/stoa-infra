"""The GitHub OIDC role that publishes stoa-frontend to one preview distribution.

`stoa-github-frontend-preview` is separate from production's
`stoa-github-frontend-deploy` (which is not managed here). The split of checks:

- GitHub decides which branch may run in the `preview-planet` Environment
  (its deployment branch rule allows only `redesign/planet`).
- AWS decides which identity may assume this role (the Environment subject,
  matched exactly) and what that identity may touch (the preview bucket and
  distribution, and only the calls the publisher makes).

The permissions are exactly the AWS calls of stoa-frontend's
`scripts/publish-web-release.mjs`: `get-bucket-versioning` on the bucket,
`put-object` on its objects, `create-invalidation` on the distribution. No
object reads, no deletes, no key value store access (the Basic Auth credential
is written by a person), and nothing in production.
"""

from __future__ import annotations

from aws_cdk import (
    Stack,
    aws_cloudfront as cloudfront,
    aws_iam as iam,
    aws_s3 as s3,
)
from constructs import Construct


GITHUB_OIDC_ISSUER = "token.actions.githubusercontent.com"
FRONTEND_REPOSITORY = "stoasystem/stoa-frontend"


class FrontendPreviewPublisherStack(Stack):
    """One exact-subject OIDC role scoped to one preview bucket and distribution."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment_name: str,
        web_bucket: s3.IBucket,
        distribution: cloudfront.IDistribution,
        role_name: str = "stoa-github-frontend-preview",
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not environment_name or any(c in environment_name for c in "*?:"):
            raise ValueError(f"not an exact GitHub Environment name: {environment_name!r}")

        provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self,
            "GitHubOidcProvider",
            f"arn:aws:iam::{self.account}:oidc-provider/{GITHUB_OIDC_ISSUER}",
        )
        self.role = iam.Role(
            self,
            "PublisherRole",
            role_name=role_name,
            description=(
                f"Publishes {FRONTEND_REPOSITORY} to the {environment_name} "
                "preview only"
            ),
            assumed_by=iam.OpenIdConnectPrincipal(
                provider,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_OIDC_ISSUER}:aud": "sts.amazonaws.com",
                        f"{GITHUB_OIDC_ISSUER}:sub": (
                            f"repo:{FRONTEND_REPOSITORY}:environment:{environment_name}"
                        ),
                    }
                },
            ),
        )

        # requireVersioning(): the publisher refuses an unversioned bucket.
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetBucketVersioning"],
                resources=[web_bucket.bucket_arn],
            )
        )
        # putObject(): every dist file, then index.html, runtime-config.json
        # and served-release.json. The version id comes back in the response.
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[web_bucket.arn_for_objects("*")],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudfront:CreateInvalidation"],
                resources=[distribution.distribution_arn],
            )
        )
