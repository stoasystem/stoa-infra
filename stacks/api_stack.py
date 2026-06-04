"""API Gateway HTTP API + Lambda (FastAPI/Mangum) + WAF."""
from aws_cdk import (
    AssetHashType,
    CfnOutput,
    Stack,
    Duration,
    aws_lambda as lambda_,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_apigatewayv2_authorizers as authorizers,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_iam as iam,
    aws_scheduler as scheduler,
)
from constructs import Construct

from stacks.lambda_dist_guard import verify_lambda_dist


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        user_pool: cognito.UserPool,
        student_client: cognito.UserPoolClient,
        parent_client: cognito.UserPoolClient,
        teacher_client: cognito.UserPoolClient,
        admin_client: cognito.UserPoolClient,
        table: dynamodb.Table,
        images_bucket: s3.Bucket,
        reports_bucket: s3.Bucket,
        teacher_queue: sqs.Queue,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        lambda_dist = verify_lambda_dist()
        lambda_code = lambda_.Code.from_asset(
            str(lambda_dist.path),
            asset_hash=lambda_dist.asset_hash,
            asset_hash_type=AssetHashType.CUSTOM,
        )

        # Lambda function — FastAPI via Mangum
        self.api_function = lambda_.Function(
            self,
            "StoaApiFunction",
            function_name="stoa-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="stoa.main.handler",
            code=lambda_code,
            memory_size=512,
            timeout=Duration.seconds(29),
            environment={
                "ENVIRONMENT": "production",
                "DYNAMODB_TABLE_NAME": table.table_name,
                "S3_IMAGES_BUCKET": images_bucket.bucket_name,
                "S3_REPORTS_BUCKET": reports_bucket.bucket_name,
                "TEACHER_QUEUE_URL": teacher_queue.queue_url,
                "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                "COGNITO_STUDENT_CLIENT_ID": student_client.user_pool_client_id,
                "COGNITO_PARENT_CLIENT_ID": parent_client.user_pool_client_id,
                "COGNITO_TEACHER_CLIENT_ID": teacher_client.user_pool_client_id,
                "COGNITO_ADMIN_CLIENT_ID": admin_client.user_pool_client_id,
                "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-6",
            },
        )

        # Grant permissions
        table.grant_read_write_data(self.api_function)
        images_bucket.grant_read_write(self.api_function)
        self._grant_report_artifact_read_write(reports_bucket, self.api_function)
        teacher_queue.grant_send_messages(self.api_function)

        self.weekly_report_function = lambda_.Function(
            self,
            "StoaWeeklyReportFunction",
            function_name="stoa-weekly-report",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="stoa.jobs.weekly_reports.handler",
            code=lambda_code,
            memory_size=1024,
            timeout=Duration.minutes(15),
            environment={
                "ENVIRONMENT": "production",
                "DYNAMODB_TABLE_NAME": table.table_name,
                "S3_REPORTS_BUCKET": reports_bucket.bucket_name,
                "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                "COGNITO_PARENT_CLIENT_ID": parent_client.user_pool_client_id,
                "COGNITO_STUDENT_CLIENT_ID": student_client.user_pool_client_id,
                "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-6",
            },
        )

        table.grant_read_write_data(self.weekly_report_function)
        self._grant_report_artifact_read_write(reports_bucket, self.weekly_report_function)

        iam.CfnPolicy(
            self,
            "GithubBackendLambdaUpdatePolicy",
            policy_name="stoa-github-backend-lambda-update",
            roles=["stoa-github-backend-deploy"],
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "lambda:GetFunctionConfiguration",
                            "lambda:UpdateFunctionCode",
                        ],
                        "Resource": [
                            self.api_function.function_arn,
                            self.weekly_report_function.function_arn,
                        ],
                    }
                ],
            },
        )

        # Bedrock & Rekognition permissions
        self.api_function.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["*"],
        ))
        self.weekly_report_function.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["*"],
        ))
        self.weekly_report_function.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            resources=["*"],
        ))
        self.api_function.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            resources=[
                f"arn:aws:ses:{self.region}:{self.account}:identity/stoaedu.ch",
            ],
        ))

        weekly_report_dlq = sqs.Queue(
            self,
            "WeeklyReportDLQ",
            queue_name="stoa-weekly-report-dlq",
            retention_period=Duration.days(14),
        )
        scheduler_role = iam.Role(
            self,
            "WeeklyReportSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        self.weekly_report_function.grant_invoke(scheduler_role)
        weekly_report_dlq.grant_send_messages(scheduler_role)

        scheduler.CfnSchedule(
            self,
            "WeeklyReportSchedule",
            name="stoa-weekly-report",
            group_name="stoa-schedules",
            description="Generate and send weekly parent learning reports.",
            schedule_expression="cron(0 6 ? * MON *)",
            schedule_expression_timezone="Europe/Zurich",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.weekly_report_function.function_arn,
                role_arn=scheduler_role.role_arn,
                input='{"source":"stoa.scheduler","job":"weekly_reports"}',
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_event_age_in_seconds=86_400,
                    maximum_retry_attempts=3,
                ),
                dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(
                    arn=weekly_report_dlq.queue_arn,
                ),
            ),
        )
        self.api_function.add_to_role_policy(iam.PolicyStatement(
            actions=["rekognition:DetectText"],
            resources=["*"],
        ))

        # Cognito admin operations (register, login, /auth/me)
        self.api_function.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "cognito-idp:AdminCreateUser",
                "cognito-idp:AdminSetUserPassword",
                "cognito-idp:AdminGetUser",
                "cognito-idp:AdminAddUserToGroup",
                "cognito-idp:InitiateAuth",
                "cognito-idp:GlobalSignOut",
            ],
            resources=[user_pool.user_pool_arn],
        ))

        # HTTP API with Cognito JWT authorizer — accepts tokens from all 4 app clients
        jwt_authorizer = authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[
                student_client.user_pool_client_id,
                parent_client.user_pool_client_id,
                teacher_client.user_pool_client_id,
                admin_client.user_pool_client_id,
            ],
        )

        http_api = apigwv2.HttpApi(
            self,
            "StoaHttpApi",
            api_name="stoa-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )

        lambda_integration = integrations.HttpLambdaIntegration(
            "LambdaIntegration", self.api_function
        )

        # Public routes (no auth)
        for path in ["/auth/register", "/auth/login", "/auth/refresh", "/health"]:
            http_api.add_routes(
                path=path,
                methods=[apigwv2.HttpMethod.POST, apigwv2.HttpMethod.GET],
                integration=lambda_integration,
            )

        # OPTIONS /{proxy+} — no auth, allows CORS preflight for all paths
        http_api.add_routes(
            path="/{proxy+}",
            methods=[apigwv2.HttpMethod.OPTIONS],
            integration=lambda_integration,
        )

        # All other routes — require JWT (explicitly exclude OPTIONS so preflight passes)
        http_api.add_routes(
            path="/{proxy+}",
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.POST,
                apigwv2.HttpMethod.PUT,
                apigwv2.HttpMethod.DELETE,
                apigwv2.HttpMethod.PATCH,
            ],
            integration=lambda_integration,
            authorizer=jwt_authorizer,
        )

        # WAF note: HTTP API v2 does not support direct WAF WebACL association.
        # WAF protection is applied at the CloudFront layer in FrontendStack (Phase 2).
        # Rate limiting is enforced by API Gateway throttling settings per stage.

        self.api_url = http_api.url
        CfnOutput(self, "ApiUrl", value=http_api.url or "", description="STOA API base URL")

    def _grant_report_artifact_read_write(
        self,
        reports_bucket: s3.Bucket,
        function: lambda_.Function,
    ) -> None:
        """Grant report artifact object access under the canonical private prefix."""
        function.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "s3:DeleteObject",
                "s3:GetObject",
                "s3:PutObject",
            ],
            resources=[reports_bucket.arn_for_objects("weekly-reports/*")],
        ))
