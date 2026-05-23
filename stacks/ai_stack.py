"""Bedrock + Rekognition permissions placeholder stack."""
from aws_cdk import Stack
from constructs import Construct


class AiStack(Stack):
    """
    Bedrock and Rekognition are accessed directly from ApiStack's Lambda role.
    This stack is reserved for future AI-specific resources:
    - Bedrock Guardrails
    - Bedrock Knowledge Base (Phase 2)
    - Custom model import
    """
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Phase 2: add BedrockGuardrail, KnowledgeBase constructs here
