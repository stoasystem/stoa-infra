"""CloudWatch dashboard, alarms, and Lambda Insights."""
from typing import Mapping, Optional

from aws_cdk import (
    Stack,
    Duration,
    aws_apigatewayv2 as apigwv2,
    aws_budgets as budgets,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from constructs import Construct

# Card 111: the address stoa-alerts mails lives outside the code. CloudFormation
# reads it at deploy time, so the parameter has to exist before the first
# deploy that carries this: `aws ssm put-parameter --name /stoa/alerts/email
# --type String --value <address>`. Changing the address is a put-parameter
# plus a deploy; the new address has to confirm its subscription.
#
# Confirm it with an AWS signature, not by clicking the email's link, so the
# footer link in every alert cannot unsubscribe it without a login
# (stoasystem/stoa-backend#47). A new subscription, or `aws sns subscribe`
# again for the same address, mails a fresh confirmation; take the Token
# parameter from its Confirm link (valid two days) and run
# `aws sns confirm-subscription --topic-arn <stoa-alerts ARN> --token <token>
# --authenticate-on-unsubscribe true`. Check with get-subscription-attributes
# that ConfirmationWasAuthenticated is "true". CloudFormation has no property
# for this, so it is lost whenever the subscription is replaced.
ALERT_EMAIL_PARAMETER = "/stoa/alerts/email"

# Card 134: the backend's request middleware publishes Requests, Errors5xx and
# Latency per feature under this namespace, with `Feature` as the only
# dimension. The keys are stoa-backend's `stoa/observability/features.py`.
# The console's health page reads these alarms back by the
# `stoa-feature-<key>-` prefix, so the names are part of that contract.
FEATURE_METRICS_NAMESPACE = "Stoa/Features"
FEATURE_P95_LIMIT_MS = {
    "auth": 1_500,
    "practice": 1_500,
    "ask_ai": 20_000,
    "teacher_help": 1_500,
    "voice": 15_000,
    "notifications": 1_000,
    "parent_reports": 3_000,
    "admin": 5_000,
}
FEATURE_ERROR_RATE_PERCENT = 2
FEATURE_MIN_REQUESTS = 20
MONTHLY_BUDGET_USD = 60
# What the backend's daily owner registry sweep logs when it has something to
# say (stoasystem/stoa-backend `src/stoa/jobs/owner_registry_sweep.py`).
OWNER_REGISTRY_SWEEP_EVENTS = (
    "owner_registry_sweep_findings",
    "owner_registry_sweep_incomplete",
    "owner_registry_sweep_marker_failed",
)
# The line stoa-api writes when Bedrock refuses a voice recording as invalid
# (stoasystem/stoa-backend `src/stoa/services/transcription.py`:
# `logger.warning("voice transcription %s", "refused" ...)`). The function logs
# in Lambda's Text format, which writes the message and drops `extra`, so the
# `event: voice_transcription_refused` the backend attaches never reaches
# CloudWatch; the message is what there is to match. Every other transcription
# failure writes "voice transcription failed". A test reads the backend checkout.
VOICE_TRANSCRIPTION_REFUSED_MESSAGE = "voice transcription refused"


class MonitoringStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        api_function: lambda_.Function,
        http_api: apigwv2.HttpApi,
        weekly_report_function: Optional[lambda_.Function] = None,
        conversation_generation_function: Optional[lambda_.Function] = None,
        dispatch_reconciler_function: Optional[lambda_.Function] = None,
        account_deletion_function: Optional[lambda_.Function] = None,
        dead_letter_queues: Mapping[str, sqs.IQueue] = {},
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        alerts_topic = sns.Topic(self, "StoaAlerts", topic_name="stoa-alerts")
        self._alerts_topic = alerts_topic
        alerts_topic.add_subscription(
            subscriptions.EmailSubscription(
                ssm.StringParameter.value_for_string_parameter(self, ALERT_EMAIL_PARAMETER)
            )
        )

        # Lambda error rate alarm
        error_alarm = cw.Alarm(
            self,
            "ApiErrorAlarm",
            alarm_name="stoa-api-error-rate",
            metric=api_function.metric_errors(period=Duration.minutes(5)),
            threshold=5,
            evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        error_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        # Lambda p99 latency alarm — tightened from 10s so a regression on a
        # critical path like login (BUG-008, ~3.9s baseline) actually pages
        # someone instead of hiding under a threshold nothing realistic hits.
        latency_alarm = cw.Alarm(
            self,
            "ApiLatencyAlarm",
            alarm_name="stoa-api-p99-latency",
            metric=api_function.metric_duration(
                statistic="p99",
                period=Duration.minutes(5),
            ),
            threshold=3_000,  # 3 seconds
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )
        latency_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        for feature, p95_limit_ms in FEATURE_P95_LIMIT_MS.items():
            self._page_on_feature(feature, p95_limit_ms)

        self._page_on_budget(alerts_topic)

        # Every job fails where nobody is watching, so any failed invocation
        # pages. #18's worker: a failure is a student waiting on an answer
        # that will not come.
        for job, function in (
            ("weekly-report", weekly_report_function),
            ("conversation-generation", conversation_generation_function),
            ("dispatch-reconciler", dispatch_reconciler_function),
            ("account-deletion", account_deletion_function),
        ):
            if function is not None:
                self._page_on_errors(job, function)

        # A message in a DLQ is work the Scheduler or SQS gave up on.
        for queue_name, queue in dead_letter_queues.items():
            self._page_on_dead_letters(queue_name, queue)

        if conversation_generation_function is not None:

            # E24: the sweep released a reservation nothing else would settle -
            # a model call whose answer was lost, or one kept when no time was
            # left to call and never retried - charging an unknown cost at the
            # ceiling. Expected to be rare; each one is worth a look.
            settled = logs.MetricFilter(
                self,
                "NeedsReconciliationSettledFilter",
                log_group=logs.LogGroup.from_log_group_name(
                    self,
                    "ConversationGenerationLogs",
                    f"/aws/lambda/{conversation_generation_function.function_name}",
                ),
                metric_namespace="Stoa/Conversations",
                metric_name="NeedsReconciliationSettled",
                filter_pattern=logs.FilterPattern.literal(
                    '"event_category=conversation_ai_needs_reconciliation_settled"'
                ),
                metric_value="1",
            )
            settled_alarm = cw.Alarm(
                self,
                "NeedsReconciliationSettledAlarm",
                alarm_name="stoa-conversation-needs-reconciliation-settled",
                metric=settled.metric(statistic="Sum", period=Duration.minutes(5)),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            settled_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        if dispatch_reconciler_function is not None:
            # stoasystem/stoa-backend#88: a student who asked for a teacher
            # nobody can be offered to, waiting past the alert limit. The sweep
            # logs one line per such request each run. The sweep's 5-minute rate
            # is not aligned to the alarm's 5-minute periods, so one period can
            # fall between two runs and hold no line: raised on any line in the
            # last two periods, the alarm stays up across that gap instead of
            # clearing and paging again. SNS writes when its state changes.
            # The event name is the backend's TEACHER_HELP_WAITING_EVENT; a test
            # reads it from the backend checkout.
            waiting = logs.MetricFilter(
                self,
                "TeacherHelpWaitingNoCandidateFilter",
                log_group=logs.LogGroup.from_log_group_name(
                    self,
                    "DispatchReconcilerLogs",
                    f"/aws/lambda/{dispatch_reconciler_function.function_name}",
                ),
                metric_namespace="Stoa/TeacherHelp",
                metric_name="WaitingWithNoTeacher",
                filter_pattern=logs.FilterPattern.literal(
                    '"event_category=teacher_help_waiting_no_candidate"'
                ),
                metric_value="1",
            )
            waiting_alarm = cw.Alarm(
                self,
                "TeacherHelpWaitingNoCandidateAlarm",
                alarm_name="stoa-teacher-help-waiting-no-teacher",
                metric=waiting.metric(statistic="Sum", period=Duration.minutes(5)),
                threshold=1,
                evaluation_periods=2,
                datapoints_to_alarm=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            waiting_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        if account_deletion_function is not None:
            # stoasystem/stoa-backend#397: the daily owner registry sweep is
            # report-only, so its log line is the only place a finding shows -
            # a row that names an account in a partition the account has not
            # registered (the registry path would not read it when that account
            # is deleted), a row a deleted account left behind, a walk that did
            # not finish, or a result that could not be recorded. The sweep runs
            # once a day, not aligned to the alarm's daily periods, so the alarm
            # is raised on any line in the last two periods and stays up while
            # the finding does. The event names are the backend's; a test reads
            # them from the backend checkout.
            #
            # `default_value=0` is what lets the alarm clear. Without it a clean
            # day publishes nothing, and CloudWatch, short of real data points,
            # reaches back over a wider evaluation range and counts every real
            # one it finds there - so a single finding held the alarm in ALARM
            # through three clean days (2026-11-21 to 23). With it every period
            # the function logs in has a real 0, and two clean days read as two
            # clean days.
            sweep_findings = logs.MetricFilter(
                self,
                "OwnerRegistrySweepFindingsFilter",
                log_group=logs.LogGroup.from_log_group_name(
                    self,
                    "AccountDeletionLogs",
                    f"/aws/lambda/{account_deletion_function.function_name}",
                ),
                metric_namespace="Stoa/AccountDeletion",
                metric_name="OwnerRegistrySweepFindings",
                filter_pattern=logs.FilterPattern.any_term(*OWNER_REGISTRY_SWEEP_EVENTS),
                metric_value="1",
                default_value=0,
            )
            sweep_alarm = cw.Alarm(
                self,
                "OwnerRegistrySweepFindingsAlarm",
                alarm_name="stoa-owner-registry-sweep-findings",
                metric=sweep_findings.metric(statistic="Sum", period=Duration.days(1)),
                threshold=1,
                evaluation_periods=2,
                datapoints_to_alarm=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            sweep_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        # stoasystem/stoa-backend#478, stoa-infra#28: Bedrock refusing a voice
        # recording as invalid - the model not taking audio in this region, or
        # not this container. Voxtral has been reported refusing audio in one
        # region; if it happens here every voice question fails and the student
        # sees only "temporarily unavailable". Normally zero, so one refusal
        # pages: low volume, and the first one is the one worth knowing about.
        refused = logs.MetricFilter(
            self,
            "VoiceTranscriptionRefusedFilter",
            log_group=logs.LogGroup.from_log_group_name(
                self,
                "ApiLogs",
                f"/aws/lambda/{api_function.function_name}",
            ),
            metric_namespace="Stoa/VoiceQuestions",
            metric_name="TranscriptionRefused",
            filter_pattern=logs.FilterPattern.literal(
                f'"{VOICE_TRANSCRIPTION_REFUSED_MESSAGE}"'
            ),
            metric_value="1",
        )
        refused_alarm = cw.Alarm(
            self,
            "VoiceTranscriptionRefusedAlarm",
            alarm_name="stoa-voice-transcription-refused",
            metric=refused.metric(statistic="Sum", period=Duration.minutes(5)),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        refused_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        # Dashboard
        dashboard = cw.Dashboard(self, "StoaDashboard", dashboard_name="STOA-Overview")
        dashboard.add_widgets(
            cw.GraphWidget(
                title="API Invocations & Errors",
                left=[api_function.metric_invocations()],
                right=[api_function.metric_errors()],
                width=12,
            ),
            cw.GraphWidget(
                title="API Latency (p50 / p99)",
                left=[
                    api_function.metric_duration(statistic="p50"),
                    api_function.metric_duration(statistic="p99"),
                ],
                width=12,
            ),
            cw.GraphWidget(
                # Separates API Gateway's own queueing/integration overhead
                # from Lambda execution time — the Lambda-only widget above
                # can't tell those apart.
                title="API Gateway Latency (p50 / p99)",
                left=[
                    http_api.metric_latency(statistic="p50"),
                    http_api.metric_latency(statistic="p99"),
                ],
                right=[
                    http_api.metric_integration_latency(statistic="p50"),
                    http_api.metric_integration_latency(statistic="p99"),
                ],
                width=12,
            ),
        )
        if weekly_report_function is not None:
            dashboard.add_widgets(
                cw.GraphWidget(
                    title="Weekly Report Job Invocations & Errors",
                    left=[weekly_report_function.metric_invocations()],
                    right=[weekly_report_function.metric_errors()],
                    width=12,
                ),
                cw.GraphWidget(
                    title="Weekly Report Job Duration (p50 / p99)",
                    left=[
                        weekly_report_function.metric_duration(statistic="p50"),
                        weekly_report_function.metric_duration(statistic="p99"),
                    ],
                    width=12,
                ),
            )

    def _page_on_feature(self, feature: str, p95_limit_ms: int) -> None:
        """Two alarms per feature: share of 5xx, and p95 latency over its limit."""

        def metric(name: str, statistic: str) -> cw.Metric:
            return cw.Metric(
                namespace=FEATURE_METRICS_NAMESPACE,
                metric_name=name,
                dimensions_map={"Feature": feature},
                statistic=statistic,
                period=Duration.minutes(5),
            )

        # Too few requests make one failure look like an outage, so the rate
        # only counts once a window has FEATURE_MIN_REQUESTS of them.
        error_rate = cw.MathExpression(
            expression=f"IF(requests >= {FEATURE_MIN_REQUESTS}, 100 * errors / requests, 0)",
            using_metrics={
                "requests": metric("Requests", "Sum"),
                "errors": metric("Errors5xx", "Sum"),
            },
            period=Duration.minutes(5),
            label=f"{feature} 5xx %",
        )
        errors = cw.Alarm(
            self,
            f"Feature{feature.title().replace('_', '')}ErrorRate",
            alarm_name=f"stoa-feature-{feature}-error-rate",
            alarm_description=f"{feature}: more than {FEATURE_ERROR_RATE_PERCENT}% of requests answered 5xx",
            metric=error_rate,
            threshold=FEATURE_ERROR_RATE_PERCENT,
            evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        errors.add_alarm_action(cw_actions.SnsAction(self._alerts_topic))
        errors.add_ok_action(cw_actions.SnsAction(self._alerts_topic))

        latency = cw.Alarm(
            self,
            f"Feature{feature.title().replace('_', '')}Latency",
            alarm_name=f"stoa-feature-{feature}-latency",
            alarm_description=f"{feature}: p95 latency above {p95_limit_ms} ms for 15 minutes",
            metric=metric("Latency", "p95"),
            threshold=p95_limit_ms,
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        latency.add_alarm_action(cw_actions.SnsAction(self._alerts_topic))
        latency.add_ok_action(cw_actions.SnsAction(self._alerts_topic))

    def _page_on_budget(self, alerts_topic: sns.Topic) -> None:
        """Mail stoa-alerts at 80% of the month's budget spent and at 100% forecast."""
        alerts_topic.add_to_resource_policy(iam.PolicyStatement(
            actions=["sns:Publish"],
            principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
            resources=[alerts_topic.topic_arn],
        ))
        subscriber = budgets.CfnBudget.SubscriberProperty(
            subscription_type="SNS", address=alerts_topic.topic_arn
        )
        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="stoa-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=MONTHLY_BUDGET_USD, unit="USD"
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type=kind,
                        comparison_operator="GREATER_THAN",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[subscriber],
                )
                for kind, threshold in (("ACTUAL", 80), ("FORECASTED", 100))
            ],
        )

    def _page_on_errors(self, job: str, function: lambda_.IFunction) -> None:
        alarm = cw.Alarm(
            self,
            f"{_pascal(job)}ErrorAlarm",
            alarm_name=f"stoa-{job}-errors",
            metric=function.metric_errors(period=Duration.minutes(5)),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(self._alerts_topic))

    def _page_on_dead_letters(self, queue_name: str, queue: sqs.IQueue) -> None:
        alarm = cw.Alarm(
            self,
            f"{_pascal(queue_name)}DlqAlarm",
            alarm_name=f"stoa-{queue_name}-dlq-messages",
            metric=queue.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(self._alerts_topic))


def _pascal(kebab: str) -> str:
    return "".join(part.capitalize() for part in kebab.split("-"))
