"""Cognito User Pool with four App Clients (student, parent, teacher, admin)."""
from aws_cdk import (
    Duration,
    Stack,
    RemovalPolicy,
    aws_cognito as cognito,
)
from constructs import Construct


class AuthStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        resource_prefix: str = "stoa",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.user_pool = cognito.UserPool(
            self,
            "StoaUserPool",
            user_pool_name=f"{resource_prefix}-users",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.RETAIN,
            custom_attributes={
                "role": cognito.StringAttribute(mutable=False),
                "grade": cognito.StringAttribute(mutable=True),
                "subscription_tier": cognito.StringAttribute(mutable=True),
            },
        )

        # One App Client per role for fine-grained scope control
        self._resource_prefix = resource_prefix
        self.student_client = self._add_client("student")
        self.parent_client = self._add_client("parent")
        self.teacher_client = self._add_client("teacher")
        self.admin_client = self._add_client("admin")

    def _add_client(self, role: str) -> cognito.UserPoolClient:
        return self.user_pool.add_client(
            f"Stoa{role.capitalize()}Client",
            user_pool_client_name=f"{self._resource_prefix}-{role}",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )
