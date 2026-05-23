"""CloudWatch dashboard, alarms, and Lambda Insights."""
from aws_cdk import (
    Stack,
    Duration,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_lambda as lambda_,
    aws_sns as sns,
)
from constructs import Construct


class MonitoringStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        api_function: lambda_.Function,
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

        # Lambda p99 latency alarm
        cw.Alarm(
            self,
            "ApiLatencyAlarm",
            alarm_name="stoa-api-p99-latency",
            metric=api_function.metric_duration(
                statistic="p99",
                period=Duration.minutes(5),
            ),
            threshold=10_000,  # 10 seconds
            evaluation_periods=3,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )

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
        )
