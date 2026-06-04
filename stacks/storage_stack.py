"""S3 buckets — homework images and weekly reports."""
from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_s3 as s3,
)
from constructs import Construct


class StorageStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Access logs bucket (private, no public access)
        self.logs_bucket = s3.Bucket(
            self,
            "StoaLogsBucket",
            bucket_name=f"stoa-access-logs-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
        )

        # Homework images (private — accessed via presigned URL)
        self.images_bucket = s3.Bucket(
            self,
            "StoaImagesBucket",
            bucket_name=f"stoa-images-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            cors=[s3.CorsRule(
                allowed_methods=[s3.HttpMethods.PUT],
                allowed_origins=["*"],
                allowed_headers=["*"],
                max_age=300,
            )],
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="images/",
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(365))],
        )

        # Weekly reports (private)
        self.reports_bucket = s3.Bucket(
            self,
            "StoaReportsBucket",
            bucket_name=f"stoa-reports-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="reports/",
            removal_policy=RemovalPolicy.RETAIN,
        )
