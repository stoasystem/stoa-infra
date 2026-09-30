"""The planet preview of the Web app, and the one identity that publishes to it.

infra#1: FrontendStack takes its domain, bucket and certificate as parameters,
and app.py declares a second, Basic-Auth-protected, noindex copy of it at
app-planet.stoaedu.ch. infra#2: stoa-github-frontend-preview may publish to
that copy and to nothing else.

The production frontend is the red line. Its template, and the template of the
release roles that read its bucket and distribution, are compared byte for
byte with the ones synthesized from main before this change.
"""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest

from stacks.frontend_stack import (
    APP_DOMAIN,
    BASIC_AUTH_KVS_KEY,
    BASIC_AUTH_FUNCTION_SOURCE,
    FrontendStack,
    WILDCARD_CERT_ARN_US,
)
from stacks.lambda_dist_guard import LambdaDistAsset


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden"
ACCOUNT = "562923011260"  # app.py's default; not a credential.
REGION = "eu-central-2"
OIDC_ISSUER = "token.actions.githubusercontent.com"

PRODUCTION_FRONTEND = "StoaFrontendStack"
PRODUCTION_DELIVERY = "StoaReleaseDeliveryStack"
PREVIEW = "StoaFrontendPreviewPlanetStack"
PUBLISHER = "StoaFrontendPreviewPublisherStack"

PREVIEW_DOMAIN = "app-planet.stoaedu.ch"
PREVIEW_BUCKET = f"stoa-frontend-preview-planet-{ACCOUNT}"
PREVIEW_SUBJECT = "repo:stoasystem/stoa-frontend:environment:preview-planet"
PROTECTED_PATHS = {"/index.html", "/served-release.json", "/runtime-config.json"}
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"


# ── Synthesis ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def synthesized(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """Run app.py the way the deploy does, with a stand-in Lambda dist."""
    dist_dir = tmp_path_factory.mktemp("lambda-dist")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "stacks.api_stack.verify_lambda_dist",
            lambda: LambdaDistAsset(path=dist_dir, asset_hash="0" * 64),
        )
        monkeypatch.delenv("STOA_LIVE_LAMBDA_ENV_FILE", raising=False)
        monkeypatch.delenv("STOA_REQUIRE_LIVE_LAMBDA_ENV", raising=False)
        app_globals = runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
        out_dir = Path(app_globals["app"].synth().directory)
        templates = {
            path.name.removesuffix(".template.json"): json.loads(path.read_text(encoding="utf-8"))
            for path in out_dir.glob("*.template.json")
        }
        yield {"globals": app_globals, "templates": templates}


@pytest.fixture(scope="module")
def templates(synthesized: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return synthesized["templates"]


@pytest.fixture(scope="module")
def preview(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return templates[PREVIEW]


@pytest.fixture(scope="module")
def publisher(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return templates[PUBLISHER]


def _resources(template: dict[str, Any], resource_type: str) -> dict[str, Any]:
    return {
        logical_id: resource
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == resource_type
    }


def _only(template: dict[str, Any], resource_type: str) -> tuple[str, dict[str, Any]]:
    matches = _resources(template, resource_type)
    assert len(matches) == 1, (resource_type, sorted(matches))
    return next(iter(matches.items()))


def _config(template: dict[str, Any]) -> dict[str, Any]:
    _, distribution = _only(template, "AWS::CloudFront::Distribution")
    return distribution["Properties"]["DistributionConfig"]


def _behaviors(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    config = _config(template)
    behaviors = {"default": config["DefaultCacheBehavior"]}
    for behavior in config.get("CacheBehaviors", []):
        behaviors[behavior["PathPattern"]] = behavior
    return behaviors


# ── Production is unchanged (red line) ────────────────────────────────────────


@pytest.mark.parametrize("stack_name", [PRODUCTION_FRONTEND, PRODUCTION_DELIVERY])
def test_production_frontend_templates_are_byte_for_byte_the_ones_from_main(
    templates: dict[str, dict[str, Any]], stack_name: str
) -> None:
    """The golden files are app.py's synthesis at 112e2df, before infra#1.

    A difference here is a change to production: to the bucket, to the
    distribution that serves app.stoaedu.ch, or to the release roles that
    write to them. Regenerate the golden files only for a change that is meant
    to reach production, and say so in the PR.
    """
    golden = json.loads((GOLDEN / f"{stack_name}.template.json").read_text(encoding="utf-8"))
    assert templates[stack_name] == golden


def test_frontend_stack_defaults_are_production() -> None:
    app = cdk.App()
    stack = FrontendStack(
        app, "DefaultsFrontend", env=cdk.Environment(account=ACCOUNT, region=REGION)
    )
    template = app.synth().get_stack_by_name("DefaultsFrontend").template

    _, bucket = _only(template, "AWS::S3::Bucket")
    assert bucket["Properties"]["BucketName"] == f"stoa-frontend-{ACCOUNT}"
    config = _config(template)
    assert config["Aliases"] == [APP_DOMAIN] == ["app.stoaedu.ch"]
    assert config["ViewerCertificate"]["AcmCertificateArn"] == WILDCARD_CERT_ARN_US
    for resource_type in (
        "AWS::CloudFront::Function",
        "AWS::CloudFront::KeyValueStore",
        "AWS::CloudFront::ResponseHeadersPolicy",
        "AWS::Route53::RecordSet",
        "AWS::CertificateManager::Certificate",
    ):
        assert _resources(template, resource_type) == {}, resource_type
    for behavior in _behaviors(template).values():
        assert "FunctionAssociations" not in behavior
        assert "ResponseHeadersPolicyId" not in behavior
    assert stack.spa_bucket is not None and stack.distribution is not None


def test_app_declares_production_frontend_without_overrides() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'frontend = FrontendStack(app, "StoaFrontendStack", env=env, tags=prod_tags)' in source


# ── The planet preview distribution (infra#1) ─────────────────────────────────


def test_preview_stack_is_in_the_production_region(synthesized: dict[str, Any]) -> None:
    stack = synthesized["globals"]["planet_preview"]
    assert stack.stack_name == PREVIEW
    assert stack.region == REGION
    assert stack.account == ACCOUNT


def test_preview_has_its_own_versioned_private_bucket(preview: dict[str, Any]) -> None:
    _, bucket = _only(preview, "AWS::S3::Bucket")
    properties = bucket["Properties"]
    assert properties["BucketName"] == PREVIEW_BUCKET
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert bucket["DeletionPolicy"] == "Retain"


def test_preview_serves_app_planet_with_the_imported_wildcard_certificate(
    preview: dict[str, Any],
) -> None:
    config = _config(preview)
    assert config["Aliases"] == [PREVIEW_DOMAIN]
    assert config["ViewerCertificate"]["AcmCertificateArn"] == WILDCARD_CERT_ARN_US
    assert WILDCARD_CERT_ARN_US.startswith("arn:aws:acm:us-east-1:")
    assert _resources(preview, "AWS::CertificateManager::Certificate") == {}


def test_preview_keeps_the_production_behaviors_and_caching(
    preview: dict[str, Any], templates: dict[str, dict[str, Any]]
) -> None:
    preview_behaviors = _behaviors(preview)
    production_behaviors = _behaviors(templates[PRODUCTION_FRONTEND])
    assert set(preview_behaviors) == set(production_behaviors) == {"default", *PROTECTED_PATHS}
    assert preview_behaviors["default"]["CachePolicyId"] == CACHING_OPTIMIZED
    for path in PROTECTED_PATHS:
        assert preview_behaviors[path]["CachePolicyId"] == CACHING_DISABLED


def test_every_preview_behavior_runs_basic_auth_on_viewer_request(
    preview: dict[str, Any],
) -> None:
    function_id, _ = _only(preview, "AWS::CloudFront::Function")
    behaviors = _behaviors(preview)
    assert set(behaviors) == {"default", *PROTECTED_PATHS}
    for name, behavior in behaviors.items():
        assert behavior.get("FunctionAssociations") == [
            {
                "EventType": "viewer-request",
                "FunctionARN": {"Fn::GetAtt": [function_id, "FunctionARN"]},
            }
        ], name


def test_basic_auth_function_reads_an_empty_key_value_store(preview: dict[str, Any]) -> None:
    kvs_id, kvs = _only(preview, "AWS::CloudFront::KeyValueStore")
    # Created empty. The password is written into it by a person, after deploy.
    assert "ImportSource" not in kvs["Properties"]
    _, function = _only(preview, "AWS::CloudFront::Function")
    config = function["Properties"]["FunctionConfig"]
    assert config["Runtime"] == "cloudfront-js-2.0"
    assert config["KeyValueStoreAssociations"] == [
        {"KeyValueStoreARN": {"Fn::GetAtt": [kvs_id, "Arn"]}}
    ]
    assert function["Properties"]["AutoPublish"] is True


def test_deployed_function_code_is_the_tested_source(preview: dict[str, Any]) -> None:
    kvs_id, _ = _only(preview, "AWS::CloudFront::KeyValueStore")
    _, function = _only(preview, "AWS::CloudFront::Function")
    parts = function["Properties"]["FunctionCode"]["Fn::Join"][1]
    rendered = "".join(
        part if isinstance(part, str) else "<KVS_ID>" for part in parts
    )
    tokens = [part for part in parts if not isinstance(part, str)]
    assert tokens == [{"Fn::GetAtt": [kvs_id, "Id"]}]
    source = BASIC_AUTH_FUNCTION_SOURCE.read_text(encoding="utf-8")
    assert rendered == source.replace("__KVS_ID__", "<KVS_ID>")


def test_basic_auth_source_never_logs_or_returns_the_secret() -> None:
    source = BASIC_AUTH_FUNCTION_SOURCE.read_text(encoding="utf-8")
    assert "console." not in source
    assert len(source.encode("utf-8")) < 10 * 1024  # CloudFront Functions limit


def test_every_preview_response_says_noindex(preview: dict[str, Any]) -> None:
    policy_id, policy = _only(preview, "AWS::CloudFront::ResponseHeadersPolicy")
    headers = policy["Properties"]["ResponseHeadersPolicyConfig"]["CustomHeadersConfig"]["Items"]
    assert headers == [{"Header": "X-Robots-Tag", "Override": True, "Value": "noindex"}]
    for name, behavior in _behaviors(preview).items():
        assert behavior.get("ResponseHeadersPolicyId") == {"Ref": policy_id}, name


def test_preview_alias_records_point_app_planet_at_the_preview_distribution(
    preview: dict[str, Any], synthesized: dict[str, Any]
) -> None:
    distribution_id, _ = _only(preview, "AWS::CloudFront::Distribution")
    records = _resources(preview, "AWS::Route53::RecordSet")
    assert {record["Properties"]["Type"] for record in records.values()} == {"A", "AAAA"}
    zone_id = synthesized["globals"]["STOAEDU_CH_HOSTED_ZONE_ID"]
    for record in records.values():
        properties = record["Properties"]
        assert properties["Name"] == f"{PREVIEW_DOMAIN}."
        assert properties["HostedZoneId"] == zone_id
        assert properties["AliasTarget"]["DNSName"] == {
            "Fn::GetAtt": [distribution_id, "DomainName"]
        }


def test_the_stoaedu_hosted_zone_id_is_filled_in(synthesized: dict[str, Any]) -> None:
    """The ALIAS goes into the existing stoaedu.ch zone, imported by id.

    The id is in no repository. Read it without writing anything:
    `aws route53 list-hosted-zones-by-name --dns-name stoaedu.ch --max-items 1`
    and put the part after /hostedzone/ into app.py. Until then this test is
    red, which keeps the deploy from creating a record in a zone that does not
    exist.
    """
    zone_id = synthesized["globals"]["STOAEDU_CH_HOSTED_ZONE_ID"]
    assert re.fullmatch(r"Z[0-9A-Z]{8,32}", zone_id), zone_id


def test_production_frontend_declares_no_dns(templates: dict[str, dict[str, Any]]) -> None:
    # app.stoaedu.ch's record lives outside CDK and stays there.
    assert _resources(templates[PRODUCTION_FRONTEND], "AWS::Route53::RecordSet") == {}


def test_preview_outputs_the_distribution_id_and_bucket_name(preview: dict[str, Any]) -> None:
    distribution_id, _ = _only(preview, "AWS::CloudFront::Distribution")
    bucket_id, _ = _only(preview, "AWS::S3::Bucket")
    outputs = preview["Outputs"]
    assert outputs["DistributionId"]["Value"] == {"Ref": distribution_id}
    assert outputs["SpaBucketName"]["Value"] == {"Ref": bucket_id}
    assert outputs["AppUrl"]["Value"] == f"https://{PREVIEW_DOMAIN}"
    rendered = json.dumps(outputs)
    assert "Authorization" not in rendered
    assert "Basic " not in rendered


def test_the_production_deploy_names_both_preview_stacks() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy-production.yml").read_text(
        encoding="utf-8"
    )
    deploy = workflow.split("cdk deploy \\", 1)[1].split("--require-approval", 1)[0]
    named = [line.strip().rstrip("\\").strip() for line in deploy.splitlines() if line.strip()]
    assert PREVIEW in named
    assert PUBLISHER in named


# ── Basic Auth behavior, run in Node ──────────────────────────────────────────

GOOD = "cHJldmlldzpjb3JyZWN0LWhvcnNl"  # base64("preview:correct-horse"), a fixture
HARNESS = r"""
import { readFileSync, writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const [source, modulePath] = process.argv.slice(2)
const code = readFileSync(source, 'utf8')
if (!code.includes("import cf from 'cloudfront';")) throw new Error('no cloudfront import')
writeFileSync(
  modulePath,
  code.replace(
    "import cf from 'cloudfront';",
    'const cf = { kvs: (id) => globalThis.__cf.kvs(id) };',
  ) +
    '\nexport { handler };\n',
)
const { handler } = await import(pathToFileURL(modulePath).href)

const store = (get) => ({
  kvs(id) {
    if (id !== '__KVS_ID__') throw new Error('unexpected kvs id ' + id)
    return { get }
  },
})
const stores = {
  missing: store(async () => { throw new Error('Key not found') }),
  broken: { kvs() { throw new Error('no store') } },
  empty: store(async () => ''),
  notString: store(async () => ({})),
  good: store(async (key) => {
    if (key !== process.env.EXPECTED_KEY) throw new Error('Key not found')
    return process.env.GOOD
  }),
}
const scenarios = JSON.parse(process.env.SCENARIOS)
const results = {}
for (const [name, { store: storeName, authorization }] of Object.entries(scenarios)) {
  globalThis.__cf = stores[storeName]
  const headers = { host: { value: 'app-planet.stoaedu.ch' } }
  if (authorization !== null) headers.authorization = { value: authorization }
  const request = { method: 'GET', uri: '/index.html', headers, querystring: {}, cookies: {} }
  const result = await handler({ version: '1.0', request })
  results[name] = result === request ? { passed: true } : result
}
process.stdout.write(JSON.stringify(results))
"""

SCENARIOS: dict[str, dict[str, Any]] = {
    "missing_key_with_credentials": {"store": "missing", "authorization": f"Basic {GOOD}"},
    "store_unavailable": {"store": "broken", "authorization": f"Basic {GOOD}"},
    "empty_value_with_empty_credentials": {"store": "empty", "authorization": "Basic "},
    "empty_value_no_header": {"store": "empty", "authorization": None},
    "non_string_value": {"store": "notString", "authorization": f"Basic {GOOD}"},
    "no_header": {"store": "good", "authorization": None},
    "wrong_password": {"store": "good", "authorization": "Basic d3Jvbmc6d3Jvbmc="},
    "right_value_wrong_scheme": {"store": "good", "authorization": f"Bearer {GOOD}"},
    "right_value_with_suffix": {"store": "good", "authorization": f"Basic {GOOD}x"},
    "correct": {"store": "good", "authorization": f"Basic {GOOD}"},
}


@pytest.fixture(scope="module")
def auth_results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    node = shutil.which("node")
    # Not a skip: without Node this gate would pass while testing nothing.
    assert node, "node is required to run the Basic Auth function tests"
    work = tmp_path_factory.mktemp("basic-auth")
    harness = work / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    completed = subprocess.run(
        [node, str(harness), str(BASIC_AUTH_FUNCTION_SOURCE), str(work / "function.mjs")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={
            "SCENARIOS": json.dumps(SCENARIOS),
            "GOOD": GOOD,
            "EXPECTED_KEY": BASIC_AUTH_KVS_KEY,
            "PATH": str(Path(node).parent),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        },
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _assert_denied(result: dict[str, Any]) -> None:
    assert result.get("statusCode") == 401, result
    headers = result["headers"]
    assert headers["www-authenticate"]["value"].startswith("Basic realm=")
    assert headers["x-robots-tag"] == {"value": "noindex"}
    assert headers["cache-control"] == {"value": "no-store"}
    assert GOOD not in json.dumps(result)


@pytest.mark.parametrize(
    "scenario",
    [
        "missing_key_with_credentials",
        "store_unavailable",
        "empty_value_with_empty_credentials",
        "empty_value_no_header",
        "non_string_value",
    ],
)
def test_basic_auth_denies_when_the_store_has_no_usable_password(
    auth_results: dict[str, Any], scenario: str
) -> None:
    _assert_denied(auth_results[scenario])


@pytest.mark.parametrize(
    "scenario", ["no_header", "wrong_password", "right_value_wrong_scheme", "right_value_with_suffix"]
)
def test_basic_auth_denies_missing_or_wrong_credentials(
    auth_results: dict[str, Any], scenario: str
) -> None:
    _assert_denied(auth_results[scenario])


def test_basic_auth_passes_the_request_through_on_the_right_credentials(
    auth_results: dict[str, Any],
) -> None:
    assert auth_results["correct"] == {"passed": True}


# ── The preview publisher role (infra#2) ──────────────────────────────────────

# Every AWS call stoa-frontend's scripts/publish-web-release.mjs makes, and the
# resource it makes it on. Nothing else: the publisher reads no objects, lists
# nothing, and reads the distribution only by invalidating it.
PUBLISHER_CALLS = {
    ("s3:GetBucketVersioning", "bucket"),  # requireVersioning()
    ("s3:PutObject", "objects"),  # putObject()
    ("cloudfront:CreateInvalidation", "distribution"),  # create-invalidation
}


def _exports(templates: dict[str, dict[str, Any]]) -> dict[str, tuple[str, Any]]:
    return {
        output["Export"]["Name"]: (stack, output["Value"])
        for stack, template in templates.items()
        for output in template.get("Outputs", {}).values()
        if "Export" in output
    }


def _resolve(value: Any, templates: dict[str, dict[str, Any]]) -> tuple[str, str, str]:
    """(stack, logical id, attribute) behind an import in the publisher stack."""
    assert isinstance(value, dict) and set(value) == {"Fn::ImportValue"}, value
    stack, exported = _exports(templates)[value["Fn::ImportValue"]]
    if "Ref" in exported:
        return stack, exported["Ref"], "Ref"
    logical_id, attribute = exported["Fn::GetAtt"]
    return stack, logical_id, attribute


def _classify(resource: Any, templates: dict[str, dict[str, Any]]) -> str:
    """Name the preview resource an IAM resource element points at, or fail."""
    preview = templates[PREVIEW]
    bucket_id, _ = _only(preview, "AWS::S3::Bucket")
    distribution_id, _ = _only(preview, "AWS::CloudFront::Distribution")
    if isinstance(resource, dict) and "Fn::ImportValue" in resource:
        assert _resolve(resource, templates) == (PREVIEW, bucket_id, "Arn")
        return "bucket"
    parts = resource["Fn::Join"][1]
    imports = [part for part in parts if isinstance(part, dict) and "Fn::ImportValue" in part]
    assert len(imports) == 1, resource
    target = _resolve(imports[0], templates)
    literal = ""
    for part in parts:
        if isinstance(part, str):
            literal += part
        elif part == {"Ref": "AWS::Partition"}:
            literal += "aws"
        elif part is imports[0]:
            literal += "<import>"
        else:
            raise AssertionError(f"unexpected token in {resource}")
    if target == (PREVIEW, bucket_id, "Arn") and literal == "<import>/*":
        return "objects"
    if target == (PREVIEW, distribution_id, "Ref") and literal == (
        f"arn:aws:cloudfront::{ACCOUNT}:distribution/<import>"
    ):
        return "distribution"
    raise AssertionError(f"not a preview resource: {resource}")


def _publisher_role(publisher: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    roles = _resources(publisher, "AWS::IAM::Role")
    matches = [
        (logical_id, role)
        for logical_id, role in roles.items()
        if role["Properties"].get("RoleName") == "stoa-github-frontend-preview"
    ]
    assert len(matches) == 1
    return matches[0]


def _publisher_statements(publisher: dict[str, Any]) -> list[dict[str, Any]]:
    role_id, _ = _publisher_role(publisher)
    policies = [
        policy
        for policy in _resources(publisher, "AWS::IAM::Policy").values()
        if {"Ref": role_id} in policy["Properties"]["Roles"]
    ]
    assert len(policies) == 1
    statements = policies[0]["Properties"]["PolicyDocument"]["Statement"]
    return statements if isinstance(statements, list) else [statements]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def test_publisher_role_trusts_only_the_preview_planet_environment(
    publisher: dict[str, Any],
) -> None:
    _, role = _publisher_role(publisher)
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    statement = trust[0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Principal"] == {
        "Federated": f"arn:aws:iam::{ACCOUNT}:oidc-provider/{OIDC_ISSUER}"
    }
    assert statement["Condition"] == {
        "StringEquals": {
            f"{OIDC_ISSUER}:aud": "sts.amazonaws.com",
            f"{OIDC_ISSUER}:sub": PREVIEW_SUBJECT,
        }
    }
    # No pattern operator and no pattern characters anywhere in the trust.
    rendered = json.dumps(statement["Condition"])
    assert "*" not in rendered and "?" not in rendered


def test_no_other_role_trusts_the_preview_environment(
    templates: dict[str, dict[str, Any]],
) -> None:
    trusting = [
        (stack, role["Properties"].get("RoleName"))
        for stack, template in templates.items()
        for role in _resources({"Resources": template.get("Resources", {})}, "AWS::IAM::Role").values()
        if "preview-planet" in json.dumps(role["Properties"]["AssumeRolePolicyDocument"])
    ]
    assert trusting == [(PUBLISHER, "stoa-github-frontend-preview")]


def test_publisher_role_may_make_exactly_the_publisher_calls_on_preview_resources(
    publisher: dict[str, Any], templates: dict[str, dict[str, Any]]
) -> None:
    granted: set[tuple[str, str]] = set()
    for statement in _publisher_statements(publisher):
        assert statement["Effect"] == "Allow"
        assert set(statement) == {"Action", "Effect", "Resource"}, statement
        for action in _as_list(statement["Action"]):
            assert "*" not in action, action
            for resource in _as_list(statement["Resource"]):
                assert resource != "*"
                granted.add((action, _classify(resource, templates)))
    assert granted == PUBLISHER_CALLS


def test_publisher_role_has_no_other_source_of_authority(publisher: dict[str, Any]) -> None:
    _, role = _publisher_role(publisher)
    properties = role["Properties"]
    assert "ManagedPolicyArns" not in properties
    assert "Policies" not in properties
    assert properties.get("PermissionsBoundary") is None
    # One role and its one policy; nothing else in the stack grants anything.
    assert sorted(resource["Type"] for resource in publisher["Resources"].values()) == [
        "AWS::IAM::Policy",
        "AWS::IAM::Role",
    ]
    actions = {
        action
        for statement in _publisher_statements(publisher)
        for action in _as_list(statement["Action"])
    }
    for forbidden in ("cloudfront-keyvaluestore:", "kms:", "s3:Delete", "s3:Get", "iam:", "sts:"):
        assert not [a for a in actions if a.startswith(forbidden) and a != "s3:GetBucketVersioning"], forbidden


def test_publisher_role_cannot_reach_production_frontend(
    publisher: dict[str, Any], templates: dict[str, dict[str, Any]]
) -> None:
    rendered = json.dumps(_publisher_statements(publisher))
    assert f"stoa-frontend-{ACCOUNT}" not in rendered
    assert f"{PRODUCTION_FRONTEND}:" not in rendered
    # Every import the role uses comes out of the preview stack.
    for match in re.finditer(r'"Fn::ImportValue": "([^"]+)"', rendered):
        assert _exports(templates)[match.group(1)][0] == PREVIEW
