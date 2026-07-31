#!/usr/bin/env python3
"""STOA CDK App — eu-central-2 (Zurich)."""
import aws_cdk as cdk

from stacks.auth_stack import AuthStack
from stacks.database_stack import DatabaseStack
from stacks.storage_stack import StorageStack
from stacks.api_stack import ApiStack
from stacks.ai_stack import AiStack
from stacks.notification_stack import NotificationStack
from stacks.monitoring_stack import MonitoringStack
from stacks.frontend_stack import FrontendStack
from stacks.release_delivery_stack import ReleaseDeliveryStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or "562923011260",
    region="eu-central-2",
)

# Shared tags applied to every resource
tags = {
    "Project": "stoa",
    "ManagedBy": "cdk",
    "Environment": app.node.try_get_context("env") or "dev",
}

auth = AuthStack(app, "StoaAuthStack", env=env, tags=tags)
database = DatabaseStack(app, "StoaDatabaseStack", env=env, tags=tags)
storage = StorageStack(app, "StoaStorageStack", env=env, tags=tags)
notification = NotificationStack(app, "StoaNotificationStack", env=env, tags=tags)
ai = AiStack(app, "StoaAiStack", env=env, tags=tags)

api = ApiStack(
    app,
    "StoaApiStack",
    user_pool=auth.user_pool,
    student_client=auth.student_client,
    parent_client=auth.parent_client,
    teacher_client=auth.teacher_client,
    admin_client=auth.admin_client,
    table=database.table,
    images_bucket=storage.images_bucket,
    reports_bucket=storage.reports_bucket,
    immutable_evidence_bucket=storage.immutable_evidence_bucket,
    teacher_queue=notification.teacher_queue,
    env=env,
    tags=tags,
)

monitoring = MonitoringStack(
    app,
    "StoaMonitoringStack",
    api_function=api.api_function,
    weekly_report_function=api.weekly_report_function,
    env=env,
    tags=tags,
)

frontend = FrontendStack(app, "StoaFrontendStack", env=env, tags=tags)

release_delivery = ReleaseDeliveryStack(
    app,
    "StoaReleaseDeliveryStack",
    artifact_bucket=storage.release_artifact_bucket,
    evidence_bucket=storage.release_evidence_bucket,
    web_bucket=frontend.spa_bucket,
    distribution=frontend.distribution,
    lambda_aliases=(
        api.api_staging_alias,
        api.api_production_alias,
        api.weekly_report_staging_alias,
        api.weekly_report_production_alias,
    ),
    env=env,
    tags=tags,
)

app.synth()
