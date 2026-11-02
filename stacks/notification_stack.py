"""SES + EventBridge weekly report scheduler."""
from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_ses as ses,
    aws_scheduler as scheduler,
)
from constructs import Construct


class NotificationStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        resource_prefix: str = "stoa",
        manage_ses_identity: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # SES email identity — only managed by the production stack.
        # Sandbox reuses the existing production SES identity via IAM permissions.
        if manage_ses_identity:
            email_identity = ses.EmailIdentity(
                self,
                "StoaEmailIdentity",
                identity=ses.Identity.domain("stoaedu.ch"),
            )
            email_identity.apply_removal_policy(RemovalPolicy.RETAIN)

        # EventBridge Scheduler — every Monday 06:00 UTC+1 (05:00 UTC)
        # The target Lambda ARN is injected after ApiStack deploys
        scheduler.CfnScheduleGroup(
            self,
            "StoaScheduleGroup",
            name=f"{resource_prefix}-schedules",
        )
