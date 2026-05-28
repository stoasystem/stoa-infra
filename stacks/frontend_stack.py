"""S3 + CloudFront for React SPA — deployed in eu-central-2 (Zurich)."""
from aws_cdk import (
    CfnOutput,
    Stack,
    RemovalPolicy,
    aws_certificatemanager as acm,
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


class FrontendStack(Stack):
    """S3 bucket + CloudFront distribution for the React SPA at app.stoaedu.ch."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # SPA bucket — no public access (served via CloudFront OAC only)
        self.spa_bucket = s3.Bucket(
            self,
            "StoaSpaBucket",
            bucket_name=f"stoa-frontend-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
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
            self, "WildcardCert", WILDCARD_CERT_ARN_US
        )

        self.distribution = cloudfront.Distribution(
            self,
            "StoaDistribution",
            comment=f"STOA SPA — {APP_DOMAIN}",
            domain_names=[APP_DOMAIN],
            certificate=cert,
            default_behavior=cloudfront.BehaviorOptions(
                origin=s3_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
            ),
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
            value=f"https://{APP_DOMAIN}",
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
