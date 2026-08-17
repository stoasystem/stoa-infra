#!/usr/bin/env python3
"""STOA CDK App — eu-central-2 (Zurich).

Usage:
  Production (default):
    cdk deploy --all

  Sandbox (isolated test environment, Stripe test keys, ENVIRONMENT=sandbox):
    cdk deploy --context sandbox=true StoaSandboxAuthStack StoaSandboxDatabaseStack \\
               StoaSandboxNotificationStack StoaSandboxApiStack
"""
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

is_sandbox = str(app.node.try_get_context("sandbox") or "").lower() in {"true", "1", "yes"}

env = cdk.Environment(
    account=app.node.try_get_context("account") or "562923011260",
    region="eu-central-2",
)

# ── Production stacks ─────────────────────────────────────────────────────────

prod_tags = {"Project": "stoa", "ManagedBy": "cdk", "Environment": "production"}

auth = AuthStack(app, "StoaAuthStack", resource_prefix="stoa", env=env, tags=prod_tags)
database = DatabaseStack(app, "StoaDatabaseStack", table_name="stoa-main", env=env, tags=prod_tags)
storage = StorageStack(app, "StoaStorageStack", resource_prefix="stoa", env=env, tags=prod_tags)
notification = NotificationStack(
    app,
    "StoaNotificationStack",
    resource_prefix="stoa",
    # The production SES domain (stoaedu.ch) already exists outside this stack.
    # The template still names stoa.ch, which SES no longer has, so managing
    # the identity here fails the whole production deploy.
    manage_ses_identity=False,
    env=env,
    tags=prod_tags,
)
ai = AiStack(app, "StoaAiStack", env=env, tags=prod_tags)

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
    env_name="production",
    resource_prefix="stoa",
    env=env,
    tags=prod_tags,
)

monitoring = MonitoringStack(
    app,
    "StoaMonitoringStack",
    api_function=api.api_function,
    weekly_report_function=api.weekly_report_function,
    env=env,
    tags=prod_tags,
)

frontend = FrontendStack(app, "StoaFrontendStack", env=env, tags=prod_tags)

# ── Sandbox stacks (deployed only when --context sandbox=true) ─────────────────
# Sandbox uses completely separate resources so production data is never touched.
# ENVIRONMENT=sandbox makes the backend refuse sk_live_ Stripe keys.
# Deploy: cdk deploy --context sandbox=true StoaSandboxAuthStack \
#           StoaSandboxDatabaseStack StoaSandboxNotificationStack StoaSandboxApiStack

sandbox_tags = {"Project": "stoa", "ManagedBy": "cdk", "Environment": "sandbox"}

sandbox_auth = AuthStack(
    app, "StoaSandboxAuthStack",
    resource_prefix="stoa-sandbox",
    env=env,
    tags=sandbox_tags,
)

sandbox_database = DatabaseStack(
    app, "StoaSandboxDatabaseStack",
    table_name="stoa-sandbox",
    env=env,
    tags=sandbox_tags,
)

sandbox_storage = StorageStack(app, "StoaSandboxStorageStack", resource_prefix="stoa-sandbox", env=env, tags=sandbox_tags)

sandbox_notification = NotificationStack(
    app, "StoaSandboxNotificationStack",
    resource_prefix="stoa-sandbox",
    manage_ses_identity=False,
    env=env,
    tags=sandbox_tags,
)

sandbox_api = ApiStack(
    app,
    "StoaSandboxApiStack",
    user_pool=sandbox_auth.user_pool,
    student_client=sandbox_auth.student_client,
    parent_client=sandbox_auth.parent_client,
    teacher_client=sandbox_auth.teacher_client,
    admin_client=sandbox_auth.admin_client,
    table=sandbox_database.table,
    images_bucket=sandbox_storage.images_bucket,
    reports_bucket=sandbox_storage.reports_bucket,
    immutable_evidence_bucket=sandbox_storage.immutable_evidence_bucket,
    teacher_queue=sandbox_notification.teacher_queue,
    env_name="sandbox",
    resource_prefix="stoa-sandbox",
    env=env,
    tags=sandbox_tags,
)

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
    tags=prod_tags,
)

app.synth()
