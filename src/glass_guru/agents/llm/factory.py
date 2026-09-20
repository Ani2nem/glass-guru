"""Choosing a model provider.

The model is a configuration value, not a code dependency, so that Phase 2's eval
suite can sweep several and answer with numbers whether Nova Lite is good enough per
agent. That question cannot be answered by argument, only by measurement, and
measurement needs the swap to be free.
"""

from __future__ import annotations

import os
from enum import StrEnum

from glass_guru.agents.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse
from glass_guru.agents.llm.bedrock import DEFAULT_MODEL_ID, DEFAULT_REGION, BedrockLLMProvider


class LLMMode(StrEnum):
    BEDROCK = "bedrock"
    #: Fails on any call. The default where no credentials exist, so an accidental
    #: live call is a clear error rather than a confusing timeout.
    UNAVAILABLE = "unavailable"


class UnavailableLLMProvider:
    """Refuses every call, with instructions."""

    @property
    def model_id(self) -> str:
        return "unavailable"

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMError(
            "no model provider configured. Set AWS credentials for Bedrock "
            "(GLASS_GURU_LLM=bedrock, GLASS_GURU_MODEL_ID=...), or pass a provider "
            "explicitly in tests."
        )


def aws_credentials_present() -> bool:
    """Whether boto3 can find credentials, without making a network call."""
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def build_llm(
    mode: LLMMode | str | None = None,
    *,
    model_id: str | None = None,
    region: str | None = None,
) -> LLMProvider:
    resolved = (
        LLMMode(mode or os.environ.get("GLASS_GURU_LLM", ""))
        if mode or os.environ.get("GLASS_GURU_LLM")
        else (LLMMode.BEDROCK if aws_credentials_present() else LLMMode.UNAVAILABLE)
    )

    if resolved is LLMMode.BEDROCK:
        return BedrockLLMProvider(
            model_id=model_id or os.environ.get("GLASS_GURU_MODEL_ID", DEFAULT_MODEL_ID),
            region=region or os.environ.get("AWS_REGION", DEFAULT_REGION),
        )
    return UnavailableLLMProvider()


def describe(provider: LLMProvider) -> str:
    return f"{type(provider).__name__} ({provider.model_id})"
