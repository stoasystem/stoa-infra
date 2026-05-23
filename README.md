# stoa-infra

AWS CDK v2 (Python) infrastructure for the STOA platform.

## Region: `eu-central-2` (Zurich)

> **Note:** `FrontendStack` deploys to `us-east-1` because CloudFront requires ACM certs in us-east-1.

## Stacks

| Stack | Resources |
|-------|-----------|
| `StoaAuthStack` | Cognito UserPool + 4 App Clients |
| `StoaDatabaseStack` | DynamoDB single-table + 4 GSIs |
| `StoaStorageStack` | S3 images + reports + logs buckets |
| `StoaApiStack` | Lambda (FastAPI/Mangum) + HTTP API + WAF |
| `StoaAiStack` | Bedrock / Rekognition (Phase 2 placeholder) |
| `StoaNotificationStack` | SQS FIFO + SES + EventBridge Scheduler |
| `StoaMonitoringStack` | CloudWatch Dashboard + Alarms |
| `StoaFrontendStack` | S3 SPA + CloudFront (OAC) — us-east-1 |

## Setup

```bash
uv sync
uv run cdk bootstrap aws://ACCOUNT/eu-central-2
uv run cdk synth
uv run cdk deploy --all --context env=dev
```

## Deploy single stack

```bash
uv run cdk deploy StoaDatabaseStack --context env=prod
```
