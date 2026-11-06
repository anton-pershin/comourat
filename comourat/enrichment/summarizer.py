"""Structured LLM summarization agent mechanics."""

from __future__ import annotations

import logging
from math import isfinite
from time import monotonic, perf_counter, sleep
from typing import Literal, TypeAlias, cast

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

_PROVIDER_ERROR_MESSAGE = "Summarization provider failed."
_TIMEOUT_ERROR_MESSAGE = "Summarization exceeded the overall deadline."


class SummarySuccess(BaseModel):
    """A successful provider result that passed the caller's empty-text policy."""

    outcome: Literal["success"] = "success"
    summary: str


class EmptySummary(BaseModel):
    """A successful but empty provider result rejected by the caller."""

    outcome: Literal["empty"] = "empty"
    summary: Literal[""] = ""


class SummaryFailure(BaseModel):
    """A sanitized provider or overall-deadline failure."""

    outcome: Literal["failure"] = "failure"
    error_category: Literal["provider", "timeout"]
    error_message: str


SummaryResult: TypeAlias = SummarySuccess | EmptySummary | SummaryFailure


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
        output_type=_SummaryOutput,  # type: ignore[arg-type]
        system_prompt=system_prompt,
        model_settings=model_settings,  # type: ignore[arg-type]
        retries=0,
    )


def _summary_failure(
    error_category: Literal["provider", "timeout"],
) -> SummaryFailure:
    message = (
        _TIMEOUT_ERROR_MESSAGE
        if error_category == "timeout"
        else _PROVIDER_ERROR_MESSAGE
    )
    return SummaryFailure(error_category=error_category, error_message=message)


def _validate_summary_limits(
    retries: int,
    retry_delay: float,
    overall_timeout: float,
) -> None:
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise ValueError("retries must be a non-negative integer")
    if not isfinite(overall_timeout) or overall_timeout <= 0:
        raise ValueError("overall_timeout must be positive and finite")
    if not isfinite(retry_delay) or retry_delay < 0:
        raise ValueError("retry_delay must be non-negative and finite")


def summarize(
    model: Model,
    content: str,
    *,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    model_settings: dict | None = None,
    require_non_empty: bool = True,
    retries: int = 1,
    retry_delay: float = 0.0,
    overall_timeout: float = 30.0,
) -> SummaryResult:
    """Summarize content with explicit outcomes and one overall deadline.

    ``retries`` counts attempts after the first attempt. Every attempt and
    retry delay receives only the time that remains before the same monotonic
    deadline. Provider exceptions are never included in the public result.
    """
    _validate_summary_limits(retries, retry_delay, overall_timeout)

    deadline = monotonic() + overall_timeout
    agent = _create_summary_agent(model, system_prompt, model_settings)

    for attempt in range(retries + 1):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return _summary_failure("timeout")

        try:
            result = agent.run_sync(content, timeout=remaining)
            if monotonic() >= deadline:
                return _summary_failure("timeout")
            summary = cast(_SummaryOutput, result.output).summary
            if not isinstance(summary, str):
                raise TypeError("summary output is not text")
            summary = summary.strip()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if isinstance(exc, TimeoutError) or monotonic() >= deadline:
                return _summary_failure("timeout")
            if attempt >= retries:
                return _summary_failure("provider")

            remaining = deadline - monotonic()
            if remaining <= 0:
                return _summary_failure("timeout")
            if retry_delay > 0:
                sleep(min(retry_delay, remaining))
                if monotonic() >= deadline:
                    return _summary_failure("timeout")
            continue

        if summary or not require_non_empty:
            return SummarySuccess(summary=summary)
        return EmptySummary()

    raise RuntimeError("unreachable")


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
        except Exception as exc:  # pylint: disable=broad-exception-caught
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
