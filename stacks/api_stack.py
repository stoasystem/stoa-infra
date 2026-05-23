"""API Gateway HTTP API + Lambda (FastAPI/Mangum) + WAF."""
from aws_cdk import (
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
    aws_wafv2 as wafv2,
)
from constructs import Construct


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        user_pool: cognito.UserPool,
        user_pool_client: cognito.UserPoolClient,
        table: dynamodb.Table,
        images_bucket: s3.Bucket,
        teacher_queue: sqs.Queue,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Lambda function — FastAPI via Mangum
        self.api_function = lambda_.Function(
            self,
            "StoaApiFunction",
            function_name="stoa-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="stoa.main.handler",
            code=lambda_.Code.from_asset("../backend/src"),
            memory_size=512,
            timeout=Duration.seconds(29),
            environment={
                "ENVIRONMENT": "production",
                "DYNAMODB_TABLE_NAME": table.table_name,
                "S3_IMAGES_BUCKET": images_bucket.bucket_name,
                "TEACHER_QUEUE_URL": teacher_queue.queue_url,
                "AWS_REGION_NAME": self.region,
                "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                "COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
                "BEDROCK_MODEL_ID": "anthropic.claude-haiku-20240307-v1:0",
            },
        )

        # Grant permissions
        table.grant_read_write_data(self.api_function)
        images_bucket.grant_read_write(self.api_function)
        teacher_queue.grant_send_messages(self.api_function)

        # Bedrock & Rekognition permissions
        self.api_function.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["*"],
        ))
        self.api_function.add_to_role_policy(iam.PolicyStatement(
            actions=["rekognition:DetectText"],
            resources=["*"],
        ))

        # HTTP API with Cognito JWT authorizer
        jwt_authorizer = authorizers.HttpJwtAuthorizer(
            "CognitoAuthorizer",
            jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
            jwt_audience=[user_pool_client.user_pool_client_id],
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

        # All other routes — require JWT
        http_api.add_routes(
            path="/{proxy+}",
            methods=[apigwv2.HttpMethod.ANY],
            integration=lambda_integration,
            authorizer=jwt_authorizer,
        )

        # WAF — rate limiting + managed rule groups
        waf = wafv2.CfnWebACL(
            self,
            "StoaWaf",
            name="stoa-api-waf",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="stoa-api-waf",
                sampled_requests_enabled=True,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimitPerIP",
                    priority=1,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=500,
                            aggregate_key_type="IP",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="RateLimitPerIP",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )

        wafv2.CfnWebACLAssociation(
            self,
            "WafAssociation",
            resource_arn=http_api.api_endpoint,
            web_acl_arn=waf.attr_arn,
        )
