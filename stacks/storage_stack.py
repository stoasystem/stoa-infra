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
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        resource_prefix: str = "stoa",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Access logs bucket (private, no public access)
        self.logs_bucket = s3.Bucket(
            self,
            "StoaLogsBucket",
            bucket_name=f"{resource_prefix}-access-logs-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
        )

        # Homework images (private — accessed via presigned URL)
        self.images_bucket = s3.Bucket(
            self,
            "StoaImagesBucket",
            bucket_name=f"{resource_prefix}-images-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            # The upload pipeline records the staging object's VersionId, which S3
            # only returns from a versioned bucket.
            versioned=True,
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
            bucket_name=f"{resource_prefix}-reports-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="reports/",
            # Report writes are acknowledged by version id, and the recovery path
            # for an interrupted write finds the object by listing its versions.
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Metadata-only immutable evidence manifests. Object Lock must be enabled
        # at bucket creation time, so this is intentionally a dedicated bucket.
        self.immutable_evidence_prefix = "audit-retention/"
        self.immutable_evidence_bucket = s3.Bucket(
            self,
            "StoaImmutableEvidenceBucket",
            bucket_name=f"{resource_prefix}-immutable-evidence-{self.account}",
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

        # Release artifacts use content-addressed keys under candidates/sha256/.
        # Object Lock and versioning preserve every byte identity even if a key
        # is submitted twice.  There is deliberately no lifecycle expiry: the
        # 90-day governance lock is the minimum for failed/staging candidates,
        # while current and known-good rollback versions remain retained.
        self.release_artifact_bucket = s3.Bucket(
            self,
            "StoaReleaseArtifactBucket",
            bucket_name=f"{resource_prefix}-release-artifacts-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.governance(
                Duration.days(90)
            ),
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="release-artifacts/",
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Promotion, approval, smoke, and rollback receipts are long-lived WORM
        # evidence. Seven years is the default minimum; no lifecycle rule can
        # expire the current or latest known-good rollback evidence.
        self.release_evidence_bucket = s3.Bucket(
            self,
            "StoaReleaseEvidenceBucket",
            bucket_name=f"{resource_prefix}-release-evidence-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.governance(
                Duration.days(2555)
            ),
            server_access_logs_bucket=self.logs_bucket,
            server_access_logs_prefix="release-evidence/",
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
        CfnOutput(
            self,
            "ReleaseArtifactBucketName",
            value=self.release_artifact_bucket.bucket_name,
            description="Versioned Object Lock store for content-addressed release artifacts",
        )
        CfnOutput(
            self,
            "ReleaseEvidenceBucketName",
            value=self.release_evidence_bucket.bucket_name,
            description="Long-term Object Lock store for release evidence",
        )
