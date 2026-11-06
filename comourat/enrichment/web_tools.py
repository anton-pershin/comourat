"""Shared web tools for pydantic-ai agents: web search and URL extraction.

The tool factory attaches `web_search` (DuckDuckGo lite) and `extract_url`
(trafilatura) to any agent, together with the tool-call budget guard and the
per-agent external-tool-time accumulator used for per-item LLM-vs-tool timing
splits.
"""

from __future__ import annotations

import logging
import time

from bs4 import BeautifulSoup
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.tools import RunContext
from trafilatura import extract

from comourat.fetch import FetchPolicy, Resolver, SafeHttpClient, _redact_url

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
DEFAULT_SEARCH_URL = "https://lite.duckduckgo.com/lite/"


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
    if usage_limits is None or usage_limits.tool_calls_limit is None:
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
    http_session=None,
    proxy: str | None = None,
    fetch_policy: FetchPolicy | None = None,
    resolver: Resolver | None = None,
) -> Agent[Model]:
    """Attach `web_search` and `extract_url` tools to an agent.

    The agent must have been created with `deps_type=dict` (the tools read
    usage from the run context only). Each tool updates the accumulator
    attribute `agent._comourat_tool_time_total` after every call via the shared
    `nonlocal` closure, so per-item timing stays live after tool calls.
    """
    tool_time_total = 0.0
    http_client = SafeHttpClient(
        http_session,
        fetch_policy=fetch_policy,
        proxy=proxy,
        resolver=resolver,
    )

    @agent.tool  # type: ignore[arg-type]
    def extract_url(ctx: RunContext[dict], url: str) -> str:
        """Extract main article content from a URL."""

        if error_msg := _ensure_tool_call_within_buffer(
            ctx, tool_call_rejection_buffer
        ):
            return error_msg

        nonlocal tool_time_total
        t_tool = time.monotonic()
        try:
            resp = http_client.get(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            resp.raise_for_status()
            content = extract(
                resp.text, url=url, include_comments=False, include_tables=True
            )
            if content:
                return content
            return f"No extractable content found at {_redact_url(url)}"
        except Exception as e:  # pylint: disable=broad-exception-caught
            return f"Error extracting {_redact_url(url)}: {type(e).__name__}"
        finally:
            tool_time_total += time.monotonic() - t_tool
            setattr(agent, "_comourat_tool_time_total", tool_time_total)
            logger.debug(
                "extract_url %s | %.2fs (tool total %.2fs)",
                _redact_url(url),
                time.monotonic() - t_tool,
                tool_time_total,
            )

    @agent.tool  # type: ignore[arg-type]
    def web_search(ctx: RunContext[dict], query: str, max_results: int = 5) -> str:
        """Search the web for information related to a query."""

        if error_msg := _ensure_tool_call_within_buffer(
            ctx, tool_call_rejection_buffer
        ):
            return error_msg

        nonlocal tool_time_total
        t_tool = time.monotonic()
        try:
            resp = http_client.post(
                DEFAULT_SEARCH_URL,
                data={"q": query},
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            links = soup.select("a.result-link")
            results = []
            for a in links[:max_results]:
                title = a.get_text(strip=True)
                href = a.get("href", "")
                results.append(f"Title: {title}\nURL: {href}")

            if not results:
                return "No results found for the web search"
            return "\n\n".join(results)
        except Exception as e:  # pylint: disable=broad-exception-caught
            return f"Error during web search: {type(e).__name__}"
        finally:
            tool_time_total += time.monotonic() - t_tool
            setattr(agent, "_comourat_tool_time_total", tool_time_total)
            logger.debug(
                "web_search | %.2fs (tool total %.2fs)",
                time.monotonic() - t_tool,
                tool_time_total,
            )

    setattr(agent, "_comourat_tool_time_total", tool_time_total)

    return agent
