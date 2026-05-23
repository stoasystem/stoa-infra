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

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or "123456789012",
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
    user_pool_client=auth.student_client,
    table=database.table,
    images_bucket=storage.images_bucket,
    teacher_queue=notification.teacher_queue,
    env=env,
    tags=tags,
)

monitoring = MonitoringStack(
    app,
    "StoaMonitoringStack",
    api_function=api.api_function,
    env=env,
    tags=tags,
)

frontend = FrontendStack(app, "StoaFrontendStack", env=env, tags=tags)

app.synth()
