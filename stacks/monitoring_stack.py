"""CloudWatch dashboard, alarms, and Lambda Insights."""
from typing import Mapping, Optional

from aws_cdk import (
    Stack,
    Duration,
    aws_apigatewayv2 as apigwv2,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
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
ALERT_EMAIL_PARAMETER = "/stoa/alerts/email"


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
