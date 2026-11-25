"""DynamoDB single-table design with its GSIs."""
from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class DatabaseStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        table_name: str = "stoa-main",
        analytics_table_name: str = "stoa-analytics",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = dynamodb.Table(
            self,
            "StoaMainTable",
            table_name=table_name,
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.RETAIN,
        )

        # GSI-Email: look up User by email
        self.table.add_global_secondary_index(
            index_name="GSI-Email",
            partition_key=dynamodb.Attribute(name="email", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI-StudentId: list Questions by student (newest first)
        self.table.add_global_secondary_index(
            index_name="GSI-StudentId",
            partition_key=dynamodb.Attribute(name="student_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI-ParentId: look up WeeklyReports by parent + week
        self.table.add_global_secondary_index(
            index_name="GSI-ParentId",
            partition_key=dynamodb.Attribute(name="parent_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="week_start", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI-TeacherId: list TeacherSessions by teacher
        self.table.add_global_secondary_index(
            index_name="GSI-TeacherId",
            partition_key=dynamodb.Attribute(name="teacher_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="started_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI-ReviewState: teacher application review queue, oldest first.
        # Sparse by design: only teacher application versions carry `review_state`, and a
        # reviewed version moves to the approved/rejected partition, so the pending
        # partition stays proportional to the actual queue rather than to all history.
        self.table.add_global_secondary_index(
            index_name="GSI-ReviewState",
            partition_key=dynamodb.Attribute(
                name="review_state", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI-SweepPending: message commands the conversation generation sweep may
        # act on, oldest due first (stoasystem/stoa-backend#502). Sparse by design:
        # only a command in a state the sweep acts on carries `sweep_bucket`, and
        # every transition out of those states removes it, so the sweep reads the
        # waiting commands rather than the whole table. `sweep_due_at` is when the
        # sweep should next look at the command.
        self.table.add_global_secondary_index(
            index_name="GSI-SweepPending",
            partition_key=dynamodb.Attribute(
                name="sweep_bucket", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="sweep_due_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI-TopicShelf: one topic's curriculum units, lessons or exercises
        # (stoasystem/stoa-backend#513). Their rows are keyed by their own id, with
        # no topic in the key, so without it the roadmap, the lesson page and the
        # learning memory read every lesson or exercise to find one topic's.
        # Sparse by design: only curriculum unit, lesson and exercise rows carry
        # `topic_shelf` (`UNIT#<topic>`, `LESSON#<topic>`, `CHALLENGE#<topic>`);
        # `topic_order` sorts a topic's exercises by lesson, then by their order.
        # Not keyed on `topic_id`: progress and other rows carry it too.
        self.table.add_global_secondary_index(
            index_name="GSI-TopicShelf",
            partition_key=dynamodb.Attribute(
                name="topic_shelf", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="topic_order", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI-Listing: every row of one kind, newest first
        # (stoasystem/stoa-backend#613). For the lists that are not one person's:
        # an administrator's view of all the site's notifications had to scan the
        # whole table to find them. Sparse by design: only a row that belongs in
        # such a list carries `listing` (the name of the list; for notifications,
        # `notification_event`), and `listed_at` is when it was created. Not
        # keyed on `entity_type` and `created_at`: nearly every row carries
        # those, and the index would be a second copy of the table.
        self.table.add_global_secondary_index(
            index_name="GSI-Listing",
            partition_key=dynamodb.Attribute(name="listing", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="listed_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # stoa-analytics: usage events, active-day marks and probe results
        # (card 134). Kept out of stoa-main so telemetry never grows the table
        # every other read pays for. Rows expire through `ttl`.
        self.analytics_table = dynamodb.Table(
            self,
            "StoaAnalyticsTable",
            table_name=analytics_table_name,
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.RETAIN,
        )
