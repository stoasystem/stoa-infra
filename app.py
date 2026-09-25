#!/usr/bin/env python3
"""STOA CDK App — eu-central-2 (Zurich).

One environment. `cdk deploy --all` deploys it.
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
    http_api=api.http_api,
    weekly_report_function=api.weekly_report_function,
    conversation_generation_function=api.conversation_generation_function,
    conversation_generation_dlq=api.conversation_generation_dlq,
    env=env,
    tags=prod_tags,
)

frontend = FrontendStack(app, "StoaFrontendStack", env=env, tags=prod_tags)

# Card 015: there is one environment, and this used to declare a second.
#
# The shadow stacks carried the same resources under a `stoa-sandbox` prefix -
# its own user pool, its own table, its own API - and sat idle from 2026-07-31.
# Two sets of identically shaped resources is not free: a survey of the live
# account read `UserPools[0]`, got the sandbox pool, and concluded the API had
# no Cognito permissions at all. It had them, on the other pool.
#
# The stacks and their orphaned table, pool and buckets are deleted. The
# contents are kept in `stoa-docs/影子环境留档/`. One bucket outlives this:
# `stoa-sandbox-release-artifacts` holds six object-locked files until
# 2026-10-28 and cannot be removed before then.
#
# A second environment, when there is a reason for one, gets declared again
# here - deliberately, and not as a copy nobody deploys.

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
