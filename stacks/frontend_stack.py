"""S3 + CloudFront for React SPA — deployed in eu-central-2 (Zurich).

The defaults are production (app.stoaedu.ch). A preview passes its own domain
and bucket name plus a `PreviewAccess`, which adds Basic Auth, noindex and the
ALIAS records. Production gets none of those, and its template is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Stack,
    RemovalPolicy,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_s3 as s3,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct

# ACM wildcard cert ARN (us-east-1) — required by CloudFront (must be us-east-1)
WILDCARD_CERT_ARN_US = (
    "arn:aws:acm:us-east-1:562923011260:certificate/5a9fc740-7ff9-4faa-b496-81d29eb2b46c"
)

APP_DOMAIN = "app.stoaedu.ch"

# CloudFront Function source for preview Basic Auth, and the key value store
# key it reads. The store is created empty; the value is written by a person.
BASIC_AUTH_FUNCTION_SOURCE = Path(__file__).with_name("preview_basic_auth.js")
BASIC_AUTH_KVS_KEY = "basic-auth"


@dataclass(frozen=True)
class PreviewAccess:
    """What turns a FrontendStack into a preview.

    Basic Auth on every behavior, `X-Robots-Tag: noindex` on every response,
    and A/AAAA ALIAS records for the domain in an existing hosted zone that is
    imported by id (no lookup, so synth needs no AWS access).
    """

    hosted_zone_id: str
    zone_name: str


class FrontendStack(Stack):
    """S3 bucket + CloudFront distribution for the React SPA (default app.stoaedu.ch)."""

    immutable_release_prefix = "releases/sha256/"
    served_release_key = "served-release.json"

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_domain: str = APP_DOMAIN,
        bucket_name: str | None = None,
        certificate_arn: str = WILDCARD_CERT_ARN_US,
        preview: PreviewAccess | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if preview is not None and not app_domain.endswith(f".{preview.zone_name}"):
            raise ValueError(f"{app_domain} is not in the zone {preview.zone_name}")

        # SPA objects remain private and are served only through CloudFront OAC.
        # Release bytes live under the content-addressed prefix; the one stable
        # descriptor object selects exact versioned Web and runtime-config bytes.
        self.spa_bucket = s3.Bucket(
            self,
            "StoaSpaBucket",
            bucket_name=bucket_name or f"stoa-frontend-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Origin Access Control (OAC) — successor to OAI
        oac = cloudfront.S3OriginAccessControl(
            self,
            "StoaOAC",
            signing=cloudfront.Signing.SIGV4_NO_OVERRIDE,
        )

        s3_origin = origins.S3BucketOrigin.with_origin_access_control(
            self.spa_bucket,
            origin_access_control=oac,
        )

        # Import the wildcard cert (must be in us-east-1 for CloudFront)
        cert = acm.Certificate.from_certificate_arn(
            self, "WildcardCert", certificate_arn
        )

        # Production passes no preview, so its behaviors get no function
        # association and no response headers policy, exactly as before.
        protection: dict[str, object] = {}
        if preview is not None:
            protection = self._preview_protection()

        def behavior(cache_policy: cloudfront.ICachePolicy) -> cloudfront.BehaviorOptions:
            return cloudfront.BehaviorOptions(
                origin=s3_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cache_policy,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                **protection,
            )

        self.distribution = cloudfront.Distribution(
            self,
            "StoaDistribution",
            comment=f"STOA SPA — {app_domain}",
            domain_names=[app_domain],
            certificate=cert,
            default_behavior=behavior(cloudfront.CachePolicy.CACHING_OPTIMIZED),
            # index.html must never be cached by CloudFront — it references hashed JS/CSS
            # bundles, and stale caches cause blank-page errors when a deploy replaces bundles.
            additional_behaviors={
                "/index.html": behavior(cloudfront.CachePolicy.CACHING_DISABLED),
                # This is the actual same-origin descriptor consumed by the
                # Web client. Its stable key is versioned in S3, while its body
                # selects immutable release-prefix object identities.
                "/served-release.json": behavior(cloudfront.CachePolicy.CACHING_DISABLED),
                "/runtime-config.json": behavior(cloudfront.CachePolicy.CACHING_DISABLED),
            },
            error_responses=[
                # SPA fallback — all 403/404 → index.html (React Router handles routing)
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
        )

        CfnOutput(
            self, "AppUrl",
            value=f"https://{app_domain}",
            description="STOA App URL",
        )
        CfnOutput(
            self, "CloudFrontDomain",
            value=self.distribution.distribution_domain_name,
            description="CloudFront domain (for Route 53 ALIAS record)",
        )
        CfnOutput(
            self, "SpaBucketName",
            value=self.spa_bucket.bucket_name,
            description="S3 bucket for frontend assets",
        )

        if preview is None:
            return

        # Production's template has no such output and keeps not having one;
        # its distribution id is recorded in stoa-docs/DEPLOYMENT.md.
        CfnOutput(
            self, "DistributionId",
            value=self.distribution.distribution_id,
            description="CloudFront distribution to invalidate after a publish",
        )
        CfnOutput(
            self, "BasicAuthKeyValueStoreArn",
            value=self.basic_auth_store.key_value_store_arn,
            description=f"Key value store; the credential goes under '{BASIC_AUTH_KVS_KEY}'",
        )

        zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=preview.hosted_zone_id,
            zone_name=preview.zone_name,
        )
        target = route53.RecordTarget.from_alias(
            route53_targets.CloudFrontTarget(self.distribution)
        )
        route53.ARecord(self, "AliasA", zone=zone, record_name=app_domain, target=target)
        route53.AaaaRecord(self, "AliasAAAA", zone=zone, record_name=app_domain, target=target)

    def _preview_protection(self) -> dict[str, object]:
        """Basic Auth function (JS 2.0, reads an empty KVS) and noindex headers."""
        self.basic_auth_store = cloudfront.KeyValueStore(
            self,
            "BasicAuthStore",
            comment="Preview Basic Auth credential; written by a person, never by CDK",
        )
        source = BASIC_AUTH_FUNCTION_SOURCE.read_text(encoding="utf-8")
        basic_auth = cloudfront.Function(
            self,
            "BasicAuthFunction",
            code=cloudfront.FunctionCode.from_inline(
                source.replace("__KVS_ID__", self.basic_auth_store.key_value_store_id)
            ),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            key_value_store=self.basic_auth_store,
            comment="Preview Basic Auth; denies unless the KVS credential matches",
        )
        noindex = cloudfront.ResponseHeadersPolicy(
            self,
            "NoIndexHeaders",
            comment="Keep the preview out of search indexes",
            custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                custom_headers=[
                    cloudfront.ResponseCustomHeader(
                        header="X-Robots-Tag", value="noindex", override=True
                    )
                ]
            ),
        )
        return {
            "function_associations": [
                cloudfront.FunctionAssociation(
                    function=basic_auth,
                    event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                )
            ],
            "response_headers_policy": noindex,
        }
