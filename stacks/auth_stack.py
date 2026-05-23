"""Cognito User Pool with four App Clients (student, parent, teacher, admin)."""
from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_cognito as cognito,
)
from constructs import Construct


class AuthStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.user_pool = cognito.UserPool(
            self,
            "StoaUserPool",
            user_pool_name="stoa-users",
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
        )

        # Custom attributes for role and grade
        self.user_pool.add_custom_attribute(
            "role", cognito.StringAttribute(mutable=False)
        )
        self.user_pool.add_custom_attribute(
            "grade", cognito.StringAttribute(mutable=True)
        )
        self.user_pool.add_custom_attribute(
            "subscription_tier", cognito.StringAttribute(mutable=True)
        )

        # One App Client per role for fine-grained scope control
        self.student_client = self._add_client("student")
        self.parent_client = self._add_client("parent")
        self.teacher_client = self._add_client("teacher")
        self.admin_client = self._add_client("admin")

    def _add_client(self, role: str) -> cognito.UserPoolClient:
        return self.user_pool.add_client(
            f"Stoa{role.capitalize()}Client",
            user_pool_client_name=f"stoa-{role}",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            prevent_user_existence_errors=True,
            access_token_validity=cdk_duration_hours(1),
            refresh_token_validity=cdk_duration_days(30),
        )


def cdk_duration_hours(h: int):
    from aws_cdk import Duration
    return Duration.hours(h)


def cdk_duration_days(d: int):
    from aws_cdk import Duration
    return Duration.days(d)
