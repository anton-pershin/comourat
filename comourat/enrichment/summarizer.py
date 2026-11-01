"""Structured LLM summarization agent mechanics."""

from __future__ import annotations

import json
import logging
from time import perf_counter
from types import SimpleNamespace

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You summarize articles for a research-assistant pipeline.

Given the title and the article content, produce a COMPACT summary of
the article: 2-4 self-contained sentences describing what it is about.
Do not add information that is not in the content.
"""


class _SummaryOutput(BaseModel):
    """Structured output of the summarizer LLM call."""

    summary: str


def _create_summary_agent(
    model: Model,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    model_settings: dict | None = None,
) -> Agent:
    """Create an agent with structured summary output."""
    return Agent(
        model,
        output_type=_SummaryOutput,
        system_prompt=system_prompt,
        model_settings=model_settings,
        retries=0,
    )


def summarize_with_events(
    agent,
    prompt: str,
    retries: int = 1,
    label: str = "",
) -> tuple[str, list[dict]]:
    """Run one structured summarization call with bounded retries.

    Returns (summary, events). Each event is a dict with keys
    `attempt`, `outcome` ("success" or "failed"), `duration_s`, and, for
    failures, `error`. Raises RuntimeError after exhaustion.
    """
    events: list[dict] = []
    last_error = None
    for attempt in range(retries + 1):
        started_at = perf_counter()
        try:
            result = agent.run_sync(prompt)
            summary = result.output.summary
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("empty summary")
            events.append(
                {
                    "attempt": attempt + 1,
                    "outcome": "success",
                    "duration_s": perf_counter() - started_at,
                }
            )
            return summary.strip(), events
        except Exception as exc:
            last_error = exc
            events.append(
                {
                    "attempt": attempt + 1,
                    "outcome": "failed",
                    "duration_s": perf_counter() - started_at,
                    "error": exc,
                }
            )
    raise RuntimeError(
        f"summarizer failed for '{label}' after {retries + 1} attempts: {last_error}"
    )
