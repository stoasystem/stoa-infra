"""S3 + CloudFront for React SPA — deployed in us-east-1 (ACM cert requirement)."""
from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_s3 as s3,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
)
from constructs import Construct


class FrontendStack(Stack):
    """
    Must deploy in us-east-1 because CloudFront only reads ACM certs from us-east-1.
    Reference this stack from CDK App with env region='us-east-1'.
    """

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

        self.distribution = cloudfront.Distribution(
            self,
            "StoaDistribution",
            comment="STOA SPA — stoa.ch",
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
