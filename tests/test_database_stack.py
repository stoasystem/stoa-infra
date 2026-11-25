"""CDK contracts for the stoa-main table's indexes."""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.database_stack import DatabaseStack


def _table() -> dict[str, Any]:
    app = cdk.App()
    stack = DatabaseStack(
        app,
        "TestDatabase",
        env=cdk.Environment(account="111122223333", region="eu-central-2"),
    )
    template = Template.from_stack(stack).to_json()
    tables = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::DynamoDB::Table"
        and resource["Properties"]["TableName"] == "stoa-main"
    ]
    assert len(tables) == 1
    return tables[0]["Properties"]


def test_analytics_table_is_its_own_table_with_ttl() -> None:
    """Card 134: usage events never land in stoa-main."""
    app = cdk.App()
    stack = DatabaseStack(
        app,
        "TestDatabase",
        env=cdk.Environment(account="111122223333", region="eu-central-2"),
    )
    template = Template.from_stack(stack).to_json()
    analytics = [
        resource["Properties"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::DynamoDB::Table"
        and resource["Properties"]["TableName"] == "stoa-analytics"
    ]
    assert len(analytics) == 1
    assert analytics[0]["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
    assert analytics[0]["BillingMode"] == "PAY_PER_REQUEST"
    assert "GlobalSecondaryIndexes" not in analytics[0]


def test_sweep_pending_index_is_keyed_by_bucket_and_due_time() -> None:
    """The generation sweep queries waiting commands through it (stoa-backend#502)."""
    table = _table()
    indexes = {index["IndexName"]: index for index in table["GlobalSecondaryIndexes"]}
    sweep = indexes["GSI-SweepPending"]
    assert sweep["KeySchema"] == [
        {"AttributeName": "sweep_bucket", "KeyType": "HASH"},
        {"AttributeName": "sweep_due_at", "KeyType": "RANGE"},
    ]
    assert sweep["Projection"] == {"ProjectionType": "ALL"}
    types = {
        attribute["AttributeName"]: attribute["AttributeType"]
        for attribute in table["AttributeDefinitions"]
    }
    assert types["sweep_bucket"] == "S"
    assert types["sweep_due_at"] == "S"


def test_topic_shelf_index_is_keyed_by_shelf_and_order() -> None:
    """The curriculum reads find one topic's rows through it (stoa-backend#513)."""
    table = _table()
    indexes = {index["IndexName"]: index for index in table["GlobalSecondaryIndexes"]}
    shelf = indexes["GSI-TopicShelf"]
    assert shelf["KeySchema"] == [
        {"AttributeName": "topic_shelf", "KeyType": "HASH"},
        {"AttributeName": "topic_order", "KeyType": "RANGE"},
    ]
    assert shelf["Projection"] == {"ProjectionType": "ALL"}
    types = {
        attribute["AttributeName"]: attribute["AttributeType"]
        for attribute in table["AttributeDefinitions"]
    }
    assert types["topic_shelf"] == "S"
    assert types["topic_order"] == "S"


def test_listing_index_is_keyed_by_list_and_creation_time() -> None:
    """A whole site's rows of one kind are read through it (stoa-backend#613)."""
    table = _table()
    indexes = {index["IndexName"]: index for index in table["GlobalSecondaryIndexes"]}
    listing = indexes["GSI-Listing"]
    assert listing["KeySchema"] == [
        {"AttributeName": "listing", "KeyType": "HASH"},
        {"AttributeName": "listed_at", "KeyType": "RANGE"},
    ]
    assert listing["Projection"] == {"ProjectionType": "ALL"}
    types = {
        attribute["AttributeName"]: attribute["AttributeType"]
        for attribute in table["AttributeDefinitions"]
    }
    assert types["listing"] == "S"
    assert types["listed_at"] == "S"


def test_the_table_has_exactly_the_indexes_the_backend_reads() -> None:
    table = _table()
    assert sorted(index["IndexName"] for index in table["GlobalSecondaryIndexes"]) == [
        "GSI-Email",
        "GSI-Listing",
        "GSI-ParentId",
        "GSI-ReviewState",
        "GSI-StudentId",
        "GSI-SweepPending",
        "GSI-TeacherId",
        "GSI-TopicShelf",
    ]
