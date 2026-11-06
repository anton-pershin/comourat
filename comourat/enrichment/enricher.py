"""LLM enrichment agent: gather additional context from the web.

The agent mechanics live here; the prompt and the item shape belong to the
caller. Runs the agent per item with usage limits and returns an explicit
context, no-context, or sanitized failure result.
"""

from __future__ import annotations

import logging
import time
from math import isfinite
from typing import Literal, cast

from pydantic import BaseModel, Field
from pydantic_ai import Agent, AgentRunResult
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from comourat.enrichment.web_tools import _WebToolError, attach_web_tools
from comourat.fetch import FetchPolicy, Resolver
from comourat.models import (
    EmptyEnrichment,
    EnrichmentFailure,
    EnrichmentResult,
    EnrichmentSuccess,
)

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You are a research assistant gathering additional context for social media posts.

For each post provided, your task is to:
1. Use the available tools to gather relevant information:
   - `extract_url`: Fetch and extract the full content of a linked article.
   - `web_search`: Find related information online.
2. Return a list of additional context points: short self-contained facts
   obtained from the web that are NOT already evident in the post.
   Each point must stand alone, phrased like "X is ...".
3. If the web search and article fetch yield nothing new, return an empty list.

Return at most 5 context points.

Note that you have **strict tool usage limits**: one `web_search` call and three `extract_url` calls.
Be frugal and smart in your choices and do not attempt to bypass these limits.
"""

_ERROR_MESSAGES = {
    "search": "Enrichment web search failed.",
    "fetch": "Enrichment URL extraction failed.",
    "provider": "Enrichment provider failed.",
    "usage_limit": "Enrichment usage limit exceeded.",
    "timeout": "Enrichment exceeded the overall deadline.",
}


class _EnrichmentOutput(BaseModel):
    """Structured provider output kept separate from public run outcomes."""

    additional_context: list[str] = Field(
        description=(
            "Short self-contained context points with facts obtained from the web "
            "that are not already evident in the item"
        )
    )


def _enrichment_failure(
    error_category: Literal["search", "fetch", "provider", "usage_limit", "timeout"],
) -> EnrichmentFailure:
    return EnrichmentFailure(
        error_category=error_category,
        error_message=_ERROR_MESSAGES[error_category],
    )


def _validate_overall_timeout(overall_timeout: float) -> None:
    if (
        isinstance(overall_timeout, bool)
        or not isinstance(overall_timeout, (int, float))
        or not isfinite(overall_timeout)
        or overall_timeout <= 0
    ):
        raise ValueError("overall_timeout must be positive and finite")


def create_enrichment_agent(
    model: Model,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    model_settings: dict | None = None,
    retries: int | None = None,
    tool_call_rejection_buffer: int = 4,
    http_session=None,
    proxy: str | None = None,
    fetch_policy: FetchPolicy | None = None,
    resolver: Resolver | None = None,
) -> Agent:
    """Create an enrichment agent with URL extraction and web search tools."""
    agent = Agent(
        model,
        output_type=_EnrichmentOutput,
        system_prompt=system_prompt,
        model_settings=model_settings,  # type: ignore[arg-type]
        retries=retries,
        deps_type=dict,
    )
    return attach_web_tools(  # type: ignore[return-value]
        agent,  # type: ignore[arg-type]
        tool_call_rejection_buffer=tool_call_rejection_buffer,
        http_session=http_session,
        proxy=proxy,
        fetch_policy=fetch_policy,
        resolver=resolver,
        raise_on_error=True,
    )


class EnrichmentAgent:
    """Wraps a pydantic-ai agent and runs one enrichment call per prompt."""

    def __init__(
        self,
        model: Model,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        model_settings: dict | None = None,
        retries: int | None = None,
        request_limit: int = 20,
        tool_calls_limit: int = 10,
        tool_call_rejection_buffer: int = 4,
        http_session=None,
        proxy: str | None = None,
        fetch_policy: FetchPolicy | None = None,
        resolver: Resolver | None = None,
        overall_timeout: float = 120.0,
    ) -> None:
        _validate_overall_timeout(overall_timeout)
        self.overall_timeout = float(overall_timeout)
        self.agent = create_enrichment_agent(
            model,
            system_prompt=system_prompt,
            model_settings=model_settings,
            retries=retries,
            tool_call_rejection_buffer=tool_call_rejection_buffer,
            http_session=http_session,
            proxy=proxy,
            fetch_policy=fetch_policy,
            resolver=resolver,
        )
        self.request_limit = request_limit
        self.tool_calls_limit = tool_calls_limit

    def enrich(self, prompt: str) -> EnrichmentResult:
        """Run one enrichment call with explicit outcomes and one deadline."""
        started_at = time.monotonic()
        deadline = started_at + self.overall_timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _enrichment_failure("timeout")

        tools_before = getattr(self.agent, "_comourat_tool_time_total", 0.0)
        try:
            run_result: AgentRunResult = self.agent.run_sync(
                prompt,
                usage_limits=UsageLimits(
                    request_limit=self.request_limit,
                    tool_calls_limit=self.tool_calls_limit,
                ),
                timeout=remaining,
            )
            provider_output = cast(_EnrichmentOutput, run_result.output)
            output = _EnrichmentOutput(
                additional_context=provider_output.additional_context
            )
            if time.monotonic() >= deadline:
                return _enrichment_failure("timeout")
        except _WebToolError as exc:
            if time.monotonic() >= deadline:
                return _enrichment_failure("timeout")
            return _enrichment_failure(exc.error_category)
        except UsageLimitExceeded:
            if time.monotonic() >= deadline:
                return _enrichment_failure("timeout")
            return _enrichment_failure("usage_limit")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if isinstance(exc, TimeoutError) or time.monotonic() >= deadline:
                return _enrichment_failure("timeout")
            return _enrichment_failure("provider")

        run_s = time.monotonic() - started_at
        tools_s = getattr(self.agent, "_comourat_tool_time_total", 0.0) - tools_before
        logger.debug(
            "enrich | total=%.2fs tools=%.2fs llm=%.2fs",
            run_s,
            tools_s,
            run_s - tools_s,
        )
        if output.additional_context:
            return EnrichmentSuccess(additional_context=output.additional_context)
        return EmptyEnrichment()
