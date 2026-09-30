# stoa-infra

AWS CDK v2 (Python) infrastructure for the STOA platform.

## Region: `eu-central-2` (Zurich) — 所有 Stack

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
| `StoaFrontendStack` | S3 SPA + CloudFront (OAC) — eu-central-2 |
| `StoaFrontendPreviewPlanetStack` | `app-planet.stoaedu.ch` 预览：独立 S3 + CloudFront，Basic Auth（CloudFront Function + 空 KVS）、`X-Robots-Tag: noindex`、A/AAAA ALIAS |
| `StoaFrontendPreviewPublisherStack` | OIDC 角色 `stoa-github-frontend-preview`（仅 `preview-planet` Environment；只能写预览 bucket、失效预览分发） |

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
