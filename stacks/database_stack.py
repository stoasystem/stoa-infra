"""DynamoDB single-table design with 4 GSIs."""
from aws_cdk import (
    Stack,
    RemovalPolicy,
    Duration,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class DatabaseStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.table = dynamodb.Table(
            self,
            "StoaMainTable",
            table_name="stoa-main",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
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
