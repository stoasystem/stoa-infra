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
from stacks.lambda_environment import (
    checkout_origins_for,
    load_live_lambda_environment,
    merge_lambda_environment,
)


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
        immutable_evidence_bucket: s3.Bucket,
        teacher_queue: sqs.Queue,
        env_name: str = "production",
        resource_prefix: str = "stoa",
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
            function_name=f"{resource_prefix}-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="stoa.main.handler",
            code=lambda_code,
            memory_size=1024,
            timeout=Duration.seconds(29),
            # Lambda Insights is off because AWS does not publish it for arm64 in
            # eu-central-2, not because anything here lacks permission. The
            # deploy role holds AdministratorAccess and still gets AccessDenied:
            #   LambdaInsightsExtension-Arm64:25 -> "no resource-based policy
            #   allows the lambda:GetLayerVersion action"
            # while LambdaInsightsExtension:35, the x86_64 layer in the same
            # account and region, reads back fine. The layer is simply not shared
            # here. Granting IAM anything will not change that; the only way to
            # get Init Duration into CloudWatch is to move this function to
            # x86_64, which costs more per millisecond than the measurement is
            # worth for BUG-008.
            # (Should that ever be revisited: of the 17 versions CDK knows, 498 is
            # the only one with an ARM ARN for this region at all, and every other
            # value fails at synth rather than at deploy.)
            environment=merge_lambda_environment(
                {
                    "ENVIRONMENT": env_name,
                    "DYNAMODB_TABLE_NAME": table.table_name,
                    "S3_IMAGES_BUCKET": images_bucket.bucket_name,
                    "S3_REPORTS_BUCKET": reports_bucket.bucket_name,
                    "IMMUTABLE_AUDIT_STORAGE_MODE": "cdk_managed",
                    "IMMUTABLE_AUDIT_STORAGE_CDK_MANAGED": "true",
                    "IMMUTABLE_AUDIT_STORAGE_RESOURCE": immutable_evidence_bucket.bucket_name,
                    "IMMUTABLE_AUDIT_STORAGE_PREFIX": "audit-retention/",
                    "TEACHER_QUEUE_URL": teacher_queue.queue_url,
                    "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                    "COGNITO_STUDENT_CLIENT_ID": student_client.user_pool_client_id,
                    "COGNITO_PARENT_CLIENT_ID": parent_client.user_pool_client_id,
                    "COGNITO_TEACHER_CLIENT_ID": teacher_client.user_pool_client_id,
                    "COGNITO_ADMIN_CLIENT_ID": admin_client.user_pool_client_id,
                    "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-6",
                    "STRIPE_CHECKOUT_WEB_ORIGINS": checkout_origins_for(env_name),
                    "APP_BASE_URL": (
                        "https://app.stoaedu.ch" if env_name == "production" else "http://localhost:5173"
                    ),
                },
                load_live_lambda_environment(f"{resource_prefix}-api", env_name=env_name),
                env_name=env_name,
            ),
        )

        # Grant permissions
        table.grant_read_write_data(self.api_function)
        images_bucket.grant_read_write(self.api_function)
        self._grant_report_artifact_read_write(reports_bucket, self.api_function)
        self._grant_immutable_evidence_access(immutable_evidence_bucket, self.api_function)
        teacher_queue.grant_send_messages(self.api_function)

        self.weekly_report_function = lambda_.Function(
            self,
            "StoaWeeklyReportFunction",
            function_name=f"{resource_prefix}-weekly-report",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="stoa.jobs.weekly_reports.handler",
            code=lambda_code,
            memory_size=1024,
            timeout=Duration.minutes(15),
            environment=merge_lambda_environment(
                {
                    "ENVIRONMENT": env_name,
                    "DYNAMODB_TABLE_NAME": table.table_name,
                    "S3_REPORTS_BUCKET": reports_bucket.bucket_name,
                    "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                    "COGNITO_PARENT_CLIENT_ID": parent_client.user_pool_client_id,
                    "COGNITO_STUDENT_CLIENT_ID": student_client.user_pool_client_id,
                    "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-4-6",
                    "STRIPE_CHECKOUT_WEB_ORIGINS": checkout_origins_for(env_name),
                },
                load_live_lambda_environment(f"{resource_prefix}-weekly-report", env_name=env_name),
                env_name=env_name,
            ),
        )

        # The audit keyring is one keyring for the platform and is set outside
        # CDK on the API. A job that has never been deployed has no copy of it,
        # so it is carried across rather than invented here.
        api_live_environment = (
            load_live_lambda_environment(f"{resource_prefix}-api", env_name=env_name) or {}
        )
        carried_audit_keys = {
            key: api_live_environment[key]
            for key in (
                "AUTHORIZATION_AUDIT_ACTIVE_KEY",
                "AUTHORIZATION_AUDIT_ACTIVE_KEY_ID",
            )
            if key in api_live_environment
        }

        self.dispatch_reconciler_function = lambda_.Function(
            self,
            "StoaDispatchReconcilerFunction",
            function_name=f"{resource_prefix}-dispatch-reconciler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="stoa.jobs.dispatch_reconciler.handler",
            code=lambda_code,
            memory_size=512,
            timeout=Duration.minutes(5),
            environment=merge_lambda_environment(
                {
                    "ENVIRONMENT": env_name,
                    "DYNAMODB_TABLE_NAME": table.table_name,
                    # Settings refuses to build in production without the
                    # issuer and client allowlists, even for a job that
                    # authenticates nobody.
                    "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                    "COGNITO_STUDENT_CLIENT_ID": student_client.user_pool_client_id,
                    "COGNITO_PARENT_CLIENT_ID": parent_client.user_pool_client_id,
                    "COGNITO_TEACHER_CLIENT_ID": teacher_client.user_pool_client_id,
                    "COGNITO_ADMIN_CLIENT_ID": admin_client.user_pool_client_id,
                    "S3_REPORTS_BUCKET": reports_bucket.bucket_name,
                    "STRIPE_CHECKOUT_WEB_ORIGINS": checkout_origins_for(env_name),
                    **carried_audit_keys,
                },
                load_live_lambda_environment(
                    f"{resource_prefix}-dispatch-reconciler", env_name=env_name
                ),
                env_name=env_name,
            ),
        )

        # Deletion is a resumable sweep, not one request's worth of work: the
        # branches page through the table and require two clean passes. The API
        # starts it in a background task and nothing continued it, so every
        # deletion stopped after one lease with the profile still live and the
        # address still claimed. This is what continues them.
        self.account_deletion_function = lambda_.Function(
            self,
            "StoaAccountDeletionFunction",
            function_name=f"{resource_prefix}-account-deletion",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="stoa.jobs.account_deletion.handler",
            code=lambda_code,
            memory_size=1024,
            timeout=Duration.minutes(10),
            environment=merge_lambda_environment(
                {
                    "ENVIRONMENT": env_name,
                    "DYNAMODB_TABLE_NAME": table.table_name,
                    "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                    "COGNITO_STUDENT_CLIENT_ID": student_client.user_pool_client_id,
                    "COGNITO_PARENT_CLIENT_ID": parent_client.user_pool_client_id,
                    "COGNITO_TEACHER_CLIENT_ID": teacher_client.user_pool_client_id,
                    "COGNITO_ADMIN_CLIENT_ID": admin_client.user_pool_client_id,
                    "S3_REPORTS_BUCKET": reports_bucket.bucket_name,
                    "STRIPE_CHECKOUT_WEB_ORIGINS": checkout_origins_for(env_name),
                    **carried_audit_keys,
                },
                load_live_lambda_environment(
                    f"{resource_prefix}-account-deletion", env_name=env_name
                ),
                env_name=env_name,
            ),
        )

        # Release traffic is pinned to immutable published versions. Promotion and
        # rollback move aliases only after the caller validates the version
        # CodeSha256 and the alias RevisionId.
        self.api_version = self.api_function.current_version
        self.api_staging_alias = lambda_.Alias(
            self,
            "StoaApiStagingAlias",
            alias_name="staging",
            version=self.api_version,
        )
        self.api_production_alias = lambda_.Alias(
            self,
            "StoaApiProductionAlias",
            alias_name="production",
            version=self.api_version,
            # Provisioned concurrency is left off. One warm environment would
            # remove cold start from the login path (BUG-008), but this account's
            # concurrency limit cannot spare it: reserving one drops unreserved
            # concurrency below the minimum of 10 and Lambda refuses the alias,
            # rolling the stack back. Turning it on means raising the account
            # limit first.
        )
        self.weekly_report_version = self.weekly_report_function.current_version
        self.weekly_report_staging_alias = lambda_.Alias(
            self,
            "StoaWeeklyReportStagingAlias",
            alias_name="staging",
            version=self.weekly_report_version,
        )
        self.weekly_report_production_alias = lambda_.Alias(
            self,
            "StoaWeeklyReportProductionAlias",
            alias_name="production",
            version=self.weekly_report_version,
        )

        self.account_deletion_version = self.account_deletion_function.current_version
        self.account_deletion_production_alias = lambda_.Alias(
            self,
            "StoaAccountDeletionProductionAlias",
            alias_name="production",
            version=self.account_deletion_version,
        )

        self.dispatch_reconciler_version = self.dispatch_reconciler_function.current_version
        self.dispatch_reconciler_production_alias = lambda_.Alias(
            self,
            "StoaDispatchReconcilerProductionAlias",
            alias_name="production",
            version=self.dispatch_reconciler_version,
        )

        table.grant_read_write_data(self.account_deletion_function)
        images_bucket.grant_read_write(self.account_deletion_function)
        self._grant_report_artifact_read_write(reports_bucket, self.account_deletion_function)
        self._grant_immutable_evidence_access(
            immutable_evidence_bucket, self.account_deletion_function
        )
        # Deletion withdraws the sign-in it is deleting.
        self.account_deletion_function.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "cognito-idp:AdminGetUser",
                "cognito-idp:AdminDisableUser",
                "cognito-idp:AdminDeleteUser",
                "cognito-idp:AdminUserGlobalSignOut",
            ],
            resources=[user_pool.user_pool_arn],
        ))
        table.grant_read_write_data(self.dispatch_reconciler_function)
        table.grant_read_write_data(self.weekly_report_function)
        self._grant_report_artifact_read_write(reports_bucket, self.weekly_report_function)
        self.api_function.add_environment(
            "WEEKLY_REPORT_FUNCTION_NAME",
            self.weekly_report_production_alias.function_arn,
        )
        self.weekly_report_production_alias.grant_invoke(self.api_production_alias)

        # The GitHub deploy policy attaches to a pre-existing IAM role created
        # outside CDK. Only create it for environments that have that CI/CD
        # role (i.e. production). Keep this policy name: a second inline policy
        # named like the historical unmanaged UpdateFunctionCode attachment
        # would fail EntityAlreadyExists.
        if env_name == "production":
            iam.CfnPolicy(
                self,
                "GithubBackendLambdaUpdatePolicy",
                policy_name=f"{resource_prefix}-github-backend-alias-update",
                roles=[f"{resource_prefix}-github-backend-deploy"],
                policy_document={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "lambda:GetFunction",
                                "lambda:GetFunctionConfiguration",
                                "lambda:PublishVersion",
                                "lambda:UpdateFunctionCode",
                            ],
                            "Resource": [
                                self.api_function.function_arn,
                                self.weekly_report_function.function_arn,
                                self.dispatch_reconciler_function.function_arn,
                                self.account_deletion_function.function_arn,
                            ],
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "lambda:GetAlias",
                                "lambda:GetFunction",
                                "lambda:UpdateAlias",
                            ],
                            # IAM authorizes these actions against the unqualified
                            # function ARN, so alias ARNs alone deny the call.
                            "Resource": [
                                self.api_function.function_arn,
                                self.weekly_report_function.function_arn,
                                self.api_staging_alias.function_arn,
                                self.api_production_alias.function_arn,
                                self.weekly_report_staging_alias.function_arn,
                                self.weekly_report_production_alias.function_arn,
                                self.dispatch_reconciler_function.function_arn,
                                self.dispatch_reconciler_production_alias.function_arn,
                                self.account_deletion_function.function_arn,
                                self.account_deletion_production_alias.function_arn,
                            ],
                        },
                    ],
                },
            )

        # Bedrock & Rekognition permissions. Token admission counts the request
        # before invoking and fails closed, so CountTokens is required to answer
        # a question at all, not only to meter one.
        self.api_function.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:CountTokens",
            ],
            resources=["*"],
        ))
        self.weekly_report_function.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:CountTokens",
            ],
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

        account_deletion_dlq = sqs.Queue(
            self,
            "AccountDeletionDLQ",
            queue_name=f"{resource_prefix}-account-deletion-dlq",
            retention_period=Duration.days(14),
        )
        account_deletion_scheduler_role = iam.Role(
            self,
            "AccountDeletionSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        self.account_deletion_production_alias.grant_invoke(account_deletion_scheduler_role)
        account_deletion_dlq.grant_send_messages(account_deletion_scheduler_role)

        scheduler.CfnSchedule(
            self,
            "AccountDeletionSchedule",
            name=f"{resource_prefix}-account-deletion",
            group_name=f"{resource_prefix}-schedules",
            description="Continue account deletion commands that are still running.",
            # Deletion is a person asking to be gone. Five minutes is how long one
            # command waits for its next pass, not how long deletion takes.
            schedule_expression="rate(5 minutes)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.account_deletion_production_alias.function_arn,
                role_arn=account_deletion_scheduler_role.role_arn,
                input='{"source":"stoa.scheduler","job":"account_deletion","limit":25}',
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_event_age_in_seconds=3_600,
                    maximum_retry_attempts=3,
                ),
                dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(
                    arn=account_deletion_dlq.queue_arn,
                ),
            ),
        )

        weekly_report_dlq = sqs.Queue(
            self,
            "WeeklyReportDLQ",
            queue_name=f"{resource_prefix}-weekly-report-dlq",
            retention_period=Duration.days(14),
        )
        scheduler_role = iam.Role(
            self,
            "WeeklyReportSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        self.weekly_report_production_alias.grant_invoke(scheduler_role)
        weekly_report_dlq.grant_send_messages(scheduler_role)

        scheduler.CfnSchedule(
            self,
            "WeeklyReportSchedule",
            name=f"{resource_prefix}-weekly-report",
            group_name=f"{resource_prefix}-schedules",
            description="Generate and send weekly parent learning reports.",
            schedule_expression="cron(0 6 ? * MON *)",
            schedule_expression_timezone="Europe/Zurich",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.weekly_report_production_alias.function_arn,
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
        dispatch_reconciler_dlq = sqs.Queue(
            self,
            "DispatchReconcilerDLQ",
            queue_name=f"{resource_prefix}-dispatch-reconciler-dlq",
            retention_period=Duration.days(14),
        )
        dispatch_scheduler_role = iam.Role(
            self,
            "DispatchReconcilerSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        self.dispatch_reconciler_production_alias.grant_invoke(dispatch_scheduler_role)
        dispatch_reconciler_dlq.grant_send_messages(dispatch_scheduler_role)

        # A teacher has ten minutes to accept, so a sweep every five bounds how
        # long a student can be waiting on nobody.
        scheduler.CfnSchedule(
            self,
            "DispatchReconcilerSchedule",
            name=f"{resource_prefix}-dispatch-reconciler",
            group_name=f"{resource_prefix}-schedules",
            description="Re-offer teacher requests that nobody accepted.",
            schedule_expression="rate(5 minutes)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.dispatch_reconciler_production_alias.function_arn,
                role_arn=dispatch_scheduler_role.role_arn,
                input='{"source":"stoa.scheduler","job":"dispatch_reconcile"}',
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_event_age_in_seconds=600,
                    maximum_retry_attempts=2,
                ),
                dead_letter_config=scheduler.CfnSchedule.DeadLetterConfigProperty(
                    arn=dispatch_reconciler_dlq.queue_arn,
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
            api_name=f"{resource_prefix}-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                allow_headers=["Authorization", "Content-Type"],
            ),
        )
        self.http_api = http_api

        lambda_integration = integrations.HttpLambdaIntegration(
            "LambdaIntegration", self.api_production_alias
        )

        # Four public routes are deliberately absent from this list and present on
        # the deployed API: POST on /auth/email-verification/{resend,confirm} and
        # /auth/login-code/{request,confirm}. They exist outside this stack's state,
        # so declaring them here makes CloudFormation try to create what is already
        # there and the whole deploy rolls back on a 409. Bringing them under
        # management means importing them, not creating them — until then this list
        # is the routes this stack owns, which is not the same as every public route
        # the API answers. stoa-backend pins the latter from its own side.
        #
        # The full unauthenticated surface, one entry per method the handler actually
        # answers. It used to pair POST with GET for each path, which published a
        # GET /auth/register, GET /auth/login and four more that no handler serves —
        # gateway surface that existed only because the loop was convenient.
        # stoa-backend pins this same set from its side in
        # tests/test_route_authorization_inventory.py; the two must agree.
        for path, methods in [
            ("/health", [apigwv2.HttpMethod.GET]),
            ("/auth/register", [apigwv2.HttpMethod.POST]),
            ("/auth/login", [apigwv2.HttpMethod.POST]),
            ("/auth/refresh", [apigwv2.HttpMethod.POST]),
            ("/auth/forgot-password", [apigwv2.HttpMethod.POST]),
            ("/auth/reset-password", [apigwv2.HttpMethod.POST]),
            # Logout carries the token in the body precisely so it does not need the
            # authorizer. Behind it, an expired session could never be revoked, which
            # is the case that most wants revoking.
            ("/auth/logout", [apigwv2.HttpMethod.POST]),
            ("/auth/invitations/claim", [apigwv2.HttpMethod.POST]),
            ("/analytics/events", [apigwv2.HttpMethod.POST]),
            # Teacher onboarding reaches these before the candidate has any identity.
            # GET /teacher-applications is the reviewer queue and stays behind the
            # authorizer: granting it here would publish every pending candidacy.
            ("/teacher-applications", [apigwv2.HttpMethod.POST]),
            ("/teacher-applications/{application_id}/status", [apigwv2.HttpMethod.GET]),
            ("/teacher-applications/activation/claim", [apigwv2.HttpMethod.POST]),
            # Stripe signs with its own secret and cannot present a JWT. The handler
            # verifies the untouched body through Stripe's SDK and refuses outright
            # when the signing secret is unset, so the signature is the authenticator.
            ("/billing/webhooks/stripe", [apigwv2.HttpMethod.POST]),
        ]:
            http_api.add_routes(
                path=path,
                methods=methods,
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
                "s3:GetObjectVersion",
                "s3:PutObject",
            ],
            resources=[reports_bucket.arn_for_objects("weekly-reports/*")],
        ))
        # Recovering an interrupted write means listing versions, which S3
        # authorizes on the bucket rather than on the objects.
        function.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:ListBucketVersions"],
            resources=[reports_bucket.bucket_arn],
        ))

    def _grant_immutable_evidence_access(
        self,
        immutable_evidence_bucket: s3.Bucket,
        function: lambda_.Function,
    ) -> None:
        """Grant metadata-only immutable evidence access without delete permissions."""
        function.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "s3:GetObject",
                "s3:PutObject",
            ],
            resources=[immutable_evidence_bucket.arn_for_objects("audit-retention/*")],
        ))
