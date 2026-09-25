"""CloudWatch dashboard, alarms, and Lambda Insights."""
from typing import Optional

from aws_cdk import (
    Stack,
    Duration,
    aws_apigatewayv2 as apigwv2,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sqs as sqs,
)
from constructs import Construct


class MonitoringStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        api_function: lambda_.Function,
        http_api: apigwv2.HttpApi,
        weekly_report_function: Optional[lambda_.Function] = None,
        conversation_generation_function: Optional[lambda_.Function] = None,
        conversation_generation_dlq: Optional[sqs.IQueue] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        alerts_topic = sns.Topic(self, "StoaAlerts", topic_name="stoa-alerts")

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
        cw.Alarm(
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

        if weekly_report_function is not None:
            report_error_alarm = cw.Alarm(
                self,
                "WeeklyReportErrorAlarm",
                alarm_name="stoa-weekly-report-errors",
                metric=weekly_report_function.metric_errors(period=Duration.minutes(5)),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            report_error_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        # #18: the worker that writes answers outside the request. A failed
        # invocation is a student waiting on an answer that will not come; a
        # message in the sweep's DLQ is a sweep the Scheduler gave up on.
        if conversation_generation_function is not None:
            worker_error_alarm = cw.Alarm(
                self,
                "ConversationGenerationErrorAlarm",
                alarm_name="stoa-conversation-generation-errors",
                metric=conversation_generation_function.metric_errors(
                    period=Duration.minutes(5)
                ),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            worker_error_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

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
        if conversation_generation_dlq is not None:
            worker_dlq_alarm = cw.Alarm(
                self,
                "ConversationGenerationDlqAlarm",
                alarm_name="stoa-conversation-generation-dlq-messages",
                metric=conversation_generation_dlq.metric_approximate_number_of_messages_visible(
                    period=Duration.minutes(5)
                ),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            worker_dlq_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

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
