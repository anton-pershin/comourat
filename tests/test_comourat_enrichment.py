"""comourat enrichment tests: summarizer core and web_tools wiring (AC5).

Exercises the comourat enrichment surface directly with mocked LLM agents.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from comourat.enrichment.summarizer import summarize_with_events
from comourat.models import EnrichmentResult


def _agent_returning(summary: str) -> MagicMock:
    agent = MagicMock()
    agent.run_sync.return_value = SimpleNamespace(
        output=SimpleNamespace(summary=summary)
    )
    return agent


def _agent_failing(error: Exception) -> MagicMock:
    agent = MagicMock()
    agent.run_sync.side_effect = error
    return agent


class TestSummarizeWithEvents:
    def test_success_returns_summary_and_event(self):
        agent = _agent_returning("  A tidy summary. ")
        summary, events = summarize_with_events(agent, "prompt", retries=1)
        assert summary == "A tidy summary."
        assert len(events) == 1
        assert events[0]["outcome"] == "success"
        assert events[0]["attempt"] == 1

    def test_retries_then_raises_runtime_error(self):
        agent = _agent_failing(RuntimeError("llm down"))
        with pytest.raises(RuntimeError, match="after 3 attempts"):
            summarize_with_events(agent, "prompt", retries=2)
        assert agent.run_sync.call_count == 3

    def test_empty_summary_counts_as_failure(self):
        agent = _agent_returning("   ")
        with pytest.raises(RuntimeError):
            summarize_with_events(agent, "prompt", retries=0)
        assert agent.run_sync.call_count == 1
        events_attempted = agent.run_sync.call_count
        assert events_attempted == 1

    def test_second_attempt_succeeds(self):
        agent = _agent_failing(RuntimeError("transient"))
        agent.run_sync.side_effect = [
            RuntimeError("transient"),
            SimpleNamespace(output=SimpleNamespace(summary="ok")),
        ]
        summary, events = summarize_with_events(agent, "p", retries=1)
        assert summary == "ok"
        assert [e["outcome"] for e in events] == ["failed", "success"]


class TestEnrichmentResult:
    def test_additional_context_roundtrip(self):
        result = EnrichmentResult(additional_context=["point one", "point two"])
        assert result.additional_context == ["point one", "point two"]
