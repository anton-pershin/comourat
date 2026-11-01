"""Shared web tools for pydantic-ai agents: web search and URL extraction.

The tool factory attaches `web_search` (DuckDuckGo lite) and `extract_url`
(trafilatura) to any agent, together with the tool-call budget guard and the
per-agent external-tool-time accumulator used for per-item LLM-vs-tool timing
splits.
"""

from __future__ import annotations

import logging
import time

import requests
from bs4 import BeautifulSoup
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.tools import RunContext
from trafilatura import extract

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def _ensure_tool_call_within_buffer(
    ctx: RunContext[dict], tool_call_rejection_buffer: int
) -> str | None:
    """Return a refusal message when the tool-call budget is nearly reached.

    The buffer lets the model answer with the information it already has
    instead of hitting the hard UsageLimitExceeded error. When
    `tool_calls_limit` is None (unbounded run), the guard never blocks.
    """
    usage = ctx.usage
    usage_limits = ctx.usage_limits
    if usage_limits.tool_calls_limit is None:
        return None
    if usage.tool_calls + tool_call_rejection_buffer > usage_limits.tool_calls_limit:
        logger.debug("attempted to exceed tool call limit")
        return (
            "Tool call limit is exceeded. "
            "Do not call tools anymore. "
            "Answer the request based on the available information"
        )
    return None


def attach_web_tools(
    agent: Agent,
    tool_call_rejection_buffer: int = 4,
) -> Agent[Model]:
    """Attach `web_search` and `extract_url` tools to an agent.

    The agent must have been created with `deps_type=dict` (the tools read
    usage from the run context only). Each tool updates the accumulator
    attribute `agent._comourat_tool_time_total` after every call via the shared
    `nonlocal` closure, so per-item timing stays live after tool calls.
    """
    tool_time_total = 0.0

    @agent.tool
    def extract_url(ctx: RunContext[dict], url: str) -> str:
        """Extract main article content from a URL."""

        if error_msg := _ensure_tool_call_within_buffer(
            ctx, tool_call_rejection_buffer
        ):
            return error_msg

        nonlocal tool_time_total
        t_tool = time.monotonic()
        try:
            resp = requests.get(
                url, timeout=30, headers={"User-Agent": DEFAULT_USER_AGENT}
            )
            resp.raise_for_status()
            content = extract(
                resp.text, url=url, include_comments=False, include_tables=True
            )
            if content:
                return content
            return f"No extractable content found at {url}"
        except Exception as e:
            return f"Error extracting {url}: {e}"
        finally:
            tool_time_total += time.monotonic() - t_tool
            agent._comourat_tool_time_total = tool_time_total  # type: ignore[attr-defined]
            logger.debug(
                "extract_url '%s' | %.2fs (tool total %.2fs)",
                url,
                time.monotonic() - t_tool,
                tool_time_total,
            )

    @agent.tool
    def web_search(ctx: RunContext[dict], query: str, max_results: int = 5) -> str:
        """Search the web for information related to a query."""

        if error_msg := _ensure_tool_call_within_buffer(
            ctx, tool_call_rejection_buffer
        ):
            return error_msg

        nonlocal tool_time_total
        t_tool = time.monotonic()
        try:
            resp = requests.post(
                "https://lite.duckduckgo.com/lite/",
                data={"q": query},
                headers={"User-Agent": DEFAULT_USER_AGENT},
                timeout=30,
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            links = soup.select("a.result-link")
            results = []
            for a in links[:max_results]:
                title = a.get_text(strip=True)
                href = a.get("href", "")
                results.append(f"Title: {title}\nURL: {href}")

            if not results:
                return f"No results found for query: {query}"
            return "\n\n".join(results)
        except Exception as e:
            return f"Error searching for '{query}': {e}"
        finally:
            tool_time_total += time.monotonic() - t_tool
            agent._comourat_tool_time_total = tool_time_total  # type: ignore[attr-defined]
            logger.debug(
                "web_search '%s' | %.2fs (tool total %.2fs)",
                query,
                time.monotonic() - t_tool,
                tool_time_total,
            )

    agent._comourat_tool_time_total = tool_time_total  # type: ignore[attr-defined]

    return agent
