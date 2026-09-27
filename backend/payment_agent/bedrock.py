"""Bedrock chat-model factory for the Enrichment Agent.

Uses `ChatBedrockConverse` (langchain-aws) — the Bedrock variant that supports native
tool-calling, which `langchain.agents.create_agent` requires. Model: Haiku 4.5 via the
cross-region inference profile `us.anthropic.claude-haiku-4-5-20251001-v1:0` (matches the
sibling `fsi-payments-processing` `bedrock_service.py` default for dev).

Region/credentials come from the standard AWS chain (env vars, shared credentials, IAM role
for IRSA on Kanopy). No key material is read here.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Haiku 4.5, cross-region inference profile (us.). Matches the fsi-payments-processing
# reference service's default for dev. Cheap + fast enough to sit in the synchronous
# Stage-3 gate without lengthening the payment path perceptibly.
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_REGION = "us-east-1"


def bedrock_model():
    """Build the `ChatBedrockConverse` instance for the agent.

    Constructed lazily (not at import) so the service boots even if AWS creds are absent —
    the `/health` route and tests that mock the model don't require a live Bedrock session.
    """
    from langchain_aws import ChatBedrockConverse

    model_id = os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    region = os.getenv("AWS_REGION", DEFAULT_REGION)
    logger.info("Bedrock chat model: %s in %s", model_id, region)
    return ChatBedrockConverse(
        model=model_id,
        region_name=region,
        temperature=0.1,
        max_tokens=800,
    )
