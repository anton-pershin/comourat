"""Public enrichment outcome and overall-deadline contract tests."""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.test import TestModel

import comourat.enrichment.enricher as enricher_module
import comourat.enrichment.web_tools as web_tools_module
from comourat.enrichment.enricher import EnrichmentAgent, create_enrichment_agent
from comourat.enrichment.web_tools import _WebToolError, attach_web_tools
from comourat.models import (
    EmptyEnrichment,
    EnrichmentFailure,
    EnrichmentResult,
    EnrichmentSuccess,
)

ERROR_MESSAGES = {
    "search": "Enrichment web search failed.",
    "fetch": "Enrichment URL extraction failed.",
    "provider": "Enrichment provider failed.",
    "usage_limit": "Enrichment usage limit exceeded.",
    "timeout": "Enrichment exceeded the overall deadline.",
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ControlledAgent:
    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = iter(outcomes)
        self.prompts: list[str] = []
        self.timeouts: list[float | None] = []

    @property
    def call_count(self) -> int:
        return len(self.prompts)

    def run_sync(self, prompt: str, *, usage_limits, timeout: float | None = None):
        self.prompts.append(prompt)
        self.timeouts.append(timeout)
        outcome = next(self._outcomes)
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(output=SimpleNamespace(additional_context=outcome))


class ToolAgent:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, function):
        self.tools[function.__name__] = function
        return function


def _context():
    return SimpleNamespace(
        usage=SimpleNamespace(tool_calls=0),
        usage_limits=SimpleNamespace(tool_calls_limit=None),
    )


def _install_controlled_agent(
    monkeypatch: pytest.MonkeyPatch,
    agent: ControlledAgent,
    clock: FakeClock | None = None,
) -> MagicMock:
    factory = MagicMock(return_value=agent)
    monkeypatch.setattr(enricher_module, "create_enrichment_agent", factory)
    if clock is not None:
        monkeypatch.setattr(enricher_module.time, "monotonic", clock.monotonic)
    return factory


def _advance_and_return(clock: FakeClock, seconds: float, outcome: object):
    def resolve() -> object:
        clock.advance(seconds)
        return outcome

    return resolve


def _chained_tool_error(category: str, sensitive_text: str) -> _WebToolError:
    try:
        raise RuntimeError(sensitive_text)
    except RuntimeError as cause:
        try:
            raise _WebToolError(category) from cause
        except _WebToolError as error:
            return error
    raise AssertionError("unreachable")


def _assert_failure(result: EnrichmentResult, category: str) -> None:
    assert isinstance(result, EnrichmentFailure)
    assert result.outcome == "failure"
    assert result.error_category == category
    assert result.error_message == ERROR_MESSAGES[category]


def _enrichment_tool_agent(monkeypatch: pytest.MonkeyPatch, http_client: MagicMock):
    agent = ToolAgent()
    monkeypatch.setattr(
        web_tools_module, "SafeHttpClient", MagicMock(return_value=http_client)
    )
    monkeypatch.setattr(enricher_module, "Agent", MagicMock(return_value=agent))
    return create_enrichment_agent(object())


def test_context_result_and_discriminated_union_roundtrip(monkeypatch):
    agent = ControlledAgent([["point one", "point two"]])
    _install_controlled_agent(monkeypatch, agent)

    result = EnrichmentAgent(object()).enrich("prompt")

    assert isinstance(result, EnrichmentSuccess)
    assert result.outcome == "context"
    assert result.additional_context == ["point one", "point two"]
    adapter = TypeAdapter(EnrichmentResult)
    assert adapter.validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        EnrichmentSuccess(additional_context=[])


def test_real_structured_agent_returns_context_without_live_tools():
    model = TestModel(
        call_tools=[], custom_output_args={"additional_context": ["test fact"]}
    )

    result = EnrichmentAgent(model, overall_timeout=5).enrich("prompt")

    assert isinstance(result, EnrichmentSuccess)
    assert result.additional_context == ["test fact"]


def test_empty_context_is_a_distinct_successful_outcome(monkeypatch):
    agent = ControlledAgent([[]])
    _install_controlled_agent(monkeypatch, agent)

    result = EnrichmentAgent(object()).enrich("prompt")

    assert isinstance(result, EmptyEnrichment)
    assert result.outcome == "no_context"
    assert result.additional_context == []
    assert not isinstance(result, EnrichmentFailure)
    with pytest.raises(ValidationError):
        EmptyEnrichment(additional_context=["not empty"])


def test_empty_search_and_extraction_results_are_not_tool_failures(monkeypatch):
    http_client = MagicMock()
    response = MagicMock()
    response.text = "<html><body>No matching content</body></html>"
    response.raise_for_status.return_value = None
    http_client.post.return_value = response
    http_client.get.return_value = response
    monkeypatch.setattr(web_tools_module, "extract", lambda *args, **kwargs: None)
    agent = _enrichment_tool_agent(monkeypatch, http_client)

    search_result = agent.tools["web_search"](_context(), "query")
    fetch_result = agent.tools["extract_url"](_context(), "https://example.com/article")

    assert search_result == "No results found for the web search"
    assert fetch_result.startswith("No extractable content found")


@pytest.mark.parametrize(
    ("tool_name", "client_method", "category", "args"),
    [
        ("web_search", "post", "search", ("query",)),
        (
            "extract_url",
            "get",
            "fetch",
            ("https://user:secret@example.com/private?token=secret",),
        ),
    ],
)
def test_tool_exceptions_are_categorized_not_returned_as_model_text(
    monkeypatch, tool_name, client_method, category, args
):
    sensitive = "credential-secret raw-response private-prompt"
    http_client = MagicMock()
    getattr(http_client, client_method).side_effect = RuntimeError(sensitive)
    agent = _enrichment_tool_agent(monkeypatch, http_client)

    with pytest.raises(_WebToolError) as caught:
        agent.tools[tool_name](_context(), *args)

    assert caught.value.error_category == category
    assert sensitive not in str(caught.value)


@pytest.mark.parametrize("category", ["search", "fetch"])
def test_tool_failures_become_public_failures_not_empty_success(monkeypatch, category):
    error = _chained_tool_error(category, "secret-tool-response")
    agent = ControlledAgent([error])
    _install_controlled_agent(monkeypatch, agent)

    result = EnrichmentAgent(object()).enrich("prompt")

    _assert_failure(result, category)
    assert not isinstance(result, EmptyEnrichment)


@pytest.mark.parametrize(
    ("tool_name", "client_method", "category"),
    [("web_search", "post", "search"), ("extract_url", "get", "fetch")],
)
def test_real_agent_does_not_convert_tool_failure_to_no_context(
    monkeypatch, tool_name, client_method, category
):
    http_client = MagicMock()
    getattr(http_client, client_method).side_effect = RuntimeError("secret failure")
    monkeypatch.setattr(
        web_tools_module, "SafeHttpClient", MagicMock(return_value=http_client)
    )
    model = TestModel(
        call_tools=[tool_name], custom_output_args={"additional_context": []}
    )

    result = EnrichmentAgent(model, retries=0, overall_timeout=5).enrich("prompt")

    _assert_failure(result, category)
    assert not isinstance(result, EmptyEnrichment)


def test_provider_and_malformed_output_failures_are_sanitized(monkeypatch):
    sensitive_agent = ControlledAgent([RuntimeError("provider-secret")])
    _install_controlled_agent(monkeypatch, sensitive_agent)
    provider_result = EnrichmentAgent(object()).enrich("private prompt")
    _assert_failure(provider_result, "provider")

    malformed_agent = ControlledAgent(["not-a-context-list"])
    _install_controlled_agent(monkeypatch, malformed_agent)
    malformed_result = EnrichmentAgent(object()).enrich("prompt")
    _assert_failure(malformed_result, "provider")


def test_usage_limit_failure_has_a_distinct_category(monkeypatch):
    agent = ControlledAgent([UsageLimitExceeded("private usage details")])
    _install_controlled_agent(monkeypatch, agent)

    result = EnrichmentAgent(object()).enrich("prompt")

    _assert_failure(result, "usage_limit")


@pytest.mark.parametrize("category", ["search", "fetch", "provider"])
def test_public_failure_excludes_sensitive_exception_data(monkeypatch, category):
    sensitive_values = (
        "credential-secret",
        "authorization-header-secret",
        "private-prompt",
        "raw-provider-response",
        "https://user:secret@example.com/private?token=secret",
    )
    sensitive_text = " | ".join(sensitive_values)
    if category == "provider":
        error = RuntimeError(sensitive_text)
    else:
        error = _chained_tool_error(category, sensitive_text)
    agent = ControlledAgent([error])
    _install_controlled_agent(monkeypatch, agent)

    result = EnrichmentAgent(object()).enrich(sensitive_values[2])

    _assert_failure(result, category)
    assert not hasattr(result, "exception")
    assert not hasattr(result, "raw_response")
    public_text = repr(result) + result.model_dump_json()
    for sensitive_value in sensitive_values:
        assert sensitive_value not in public_text


def test_agent_run_receives_the_configured_overall_timeout(monkeypatch):
    clock = FakeClock()
    agent = ControlledAgent([["fact"]])
    _install_controlled_agent(monkeypatch, agent, clock)

    result = EnrichmentAgent(object(), overall_timeout=12).enrich("prompt")

    assert isinstance(result, EnrichmentSuccess)
    assert agent.timeouts == [12]


def test_timeout_exception_returns_sanitized_timeout_failure(monkeypatch):
    agent = ControlledAgent([TimeoutError("secret provider timeout")])
    _install_controlled_agent(monkeypatch, agent, FakeClock())

    result = EnrichmentAgent(object(), overall_timeout=10).enrich("prompt")

    _assert_failure(result, "timeout")
    assert agent.timeouts == [10]


def test_late_agent_result_cannot_become_success(monkeypatch):
    clock = FakeClock()
    agent = ControlledAgent([_advance_and_return(clock, 11, ["late fact"])])
    _install_controlled_agent(monkeypatch, agent, clock)

    result = EnrichmentAgent(object(), overall_timeout=10).enrich("prompt")

    _assert_failure(result, "timeout")
    assert agent.call_count == 1


def test_deadline_before_run_prevents_agent_call(monkeypatch):
    agent = ControlledAgent([])
    factory = _install_controlled_agent(monkeypatch, agent)
    monotonic_values = iter([0.0, 10.0])
    monkeypatch.setattr(
        enricher_module.time, "monotonic", lambda: next(monotonic_values)
    )

    result = EnrichmentAgent(object(), overall_timeout=10).enrich("prompt")

    _assert_failure(result, "timeout")
    factory.assert_called_once()
    assert agent.call_count == 0


@pytest.mark.parametrize(
    "timeout",
    [0, -1, math.nan, math.inf, -math.inf, "30", None, True, False],
)
def test_invalid_overall_timeout_is_rejected_before_agent_creation(
    monkeypatch, timeout
):
    agent = ControlledAgent([])
    factory = _install_controlled_agent(monkeypatch, agent)

    with pytest.raises(ValueError, match="overall_timeout"):
        EnrichmentAgent(object(), overall_timeout=timeout)

    factory.assert_not_called()
    assert agent.call_count == 0


def test_direct_web_tools_keep_error_strings_for_paper_discovery(monkeypatch):
    http_client = MagicMock()
    http_client.post.side_effect = RuntimeError("private search failure")
    monkeypatch.setattr(
        web_tools_module, "SafeHttpClient", MagicMock(return_value=http_client)
    )
    agent = ToolAgent()
    attach_web_tools(agent)

    result = agent.tools["web_search"](_context(), "query")

    assert result == "Error during web search: RuntimeError"
