"""SQS FIFO queue (teacher escalation) + SES + EventBridge weekly report scheduler."""
from aws_cdk import (
    Stack,
    Duration,
    aws_sqs as sqs,
    aws_ses as ses,
    aws_scheduler as scheduler,
)
from constructs import Construct


class NotificationStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Dead-letter queue for failed teacher escalations
        dlq = sqs.Queue(
            self,
            "TeacherQueueDLQ",
            queue_name="stoa-teacher-escalation-dlq.fifo",
            fifo=True,
            content_based_deduplication=True,
            retention_period=Duration.days(14),
        )

        # FIFO queue — ordered teacher escalation events
        self.teacher_queue = sqs.Queue(
            self,
            "TeacherEscalationQueue",
            queue_name="stoa-teacher-escalation.fifo",
            fifo=True,
            content_based_deduplication=True,
            visibility_timeout=Duration.seconds(60),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )

        # SES email identity — configure DNS in Route53 separately
        ses.EmailIdentity(
            self,
            "StoaEmailIdentity",
            identity=ses.Identity.domain("stoaedu.ch"),
        )

        # EventBridge Scheduler — every Monday 06:00 UTC+1 (05:00 UTC)
        # The target Lambda ARN is injected after ApiStack deploys
        scheduler.CfnScheduleGroup(
            self,
            "StoaScheduleGroup",
            name="stoa-schedules",
        )
