"""S3 buckets — homework images and weekly reports."""
from aws_cdk import (
    Stack,
    CfnOutput,
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

        # Metadata-only immutable evidence manifests. Object Lock must be enabled
        # at bucket creation time, so this is intentionally a dedicated bucket.
        self.immutable_evidence_prefix = "audit-retention/"
        self.immutable_evidence_bucket = s3.Bucket(
            self,
            "StoaImmutableEvidenceBucket",
            bucket_name=f"stoa-immutable-evidence-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.governance(
                Duration.days(365)
            ),
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="immutable-evidence/",
            removal_policy=RemovalPolicy.RETAIN,
        )

        CfnOutput(
            self,
            "ImmutableEvidenceBucketName",
            value=self.immutable_evidence_bucket.bucket_name,
            description="CDK-managed immutable evidence metadata bucket",
        )
        CfnOutput(
            self,
            "ImmutableEvidencePrefix",
            value=self.immutable_evidence_prefix,
            description="Immutable evidence metadata object prefix",
        )
        CfnOutput(
            self,
            "ImmutableEvidenceObjectLockMode",
            value="GOVERNANCE",
            description="Default S3 Object Lock retention mode for immutable evidence",
        )
        CfnOutput(
            self,
            "ImmutableEvidenceDefaultRetentionDays",
            value="365",
            description="Default S3 Object Lock retention period for immutable evidence",
        )
