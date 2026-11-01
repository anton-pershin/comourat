"""LLM enrichment agent: gather additional context from the web.

The agent mechanics live here; the prompt and the item shape belong to the
caller. Runs the agent per item with usage limits and returns the agent's
`EnrichmentResult`.
"""

from __future__ import annotations

import logging
import time

from pydantic_ai import Agent, AgentRunResult
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from comourat.enrichment.web_tools import attach_web_tools
from comourat.models import EnrichmentResult

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


def create_enrichment_agent(
    model: Model,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    model_settings: dict | None = None,
    retries: int | None = None,
    tool_call_rejection_buffer: int = 4,
) -> Agent:
    """Create an enrichment agent with URL extraction and web search tools."""
    agent = Agent(
        model,
        output_type=EnrichmentResult,
        system_prompt=system_prompt,
        model_settings=model_settings,
        retries=retries,
        deps_type=dict,
    )
    return attach_web_tools(
        agent, tool_call_rejection_buffer=tool_call_rejection_buffer
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
    ) -> None:
        self.agent = create_enrichment_agent(
            model,
            system_prompt=system_prompt,
            model_settings=model_settings,
            retries=retries,
            tool_call_rejection_buffer=tool_call_rejection_buffer,
        )
        self.request_limit = request_limit
        self.tool_calls_limit = tool_calls_limit

    def enrich(self, prompt: str) -> EnrichmentResult:
        """Run one enrichment call. Raises UsageLimitExceeded when limits hit."""
        tools_before = self.agent._comourat_tool_time_total  # type: ignore[attr-defined]
        t_run = time.monotonic()
        try:
            run_result: AgentRunResult = self.agent.run_sync(
                prompt,
                usage_limits=UsageLimits(
                    request_limit=self.request_limit,
                    tool_calls_limit=self.tool_calls_limit,
                ),
            )
        except UsageLimitExceeded as e:
            logger.exception(
                "Tool or request usage limit exceeded, skip this item: %s", e
            )
            raise
        run_s = time.monotonic() - t_run
        tools_s = (
            self.agent._comourat_tool_time_total - tools_before  # type: ignore[attr-defined]
        )
        logger.debug(
            "enrich | total=%.2fs tools=%.2fs llm=%.2fs",
            run_s,
            tools_s,
            run_s - tools_s,
        )
        return run_result.output
