"""Public summarization contract tests with controlled provider behavior."""

from __future__ import annotations

import math
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic_ai.models.test import TestModel

import comourat.enrichment.summarizer as summarizer_module
from comourat.enrichment.summarizer import (
    EmptySummary,
    SummaryFailure,
    SummarySuccess,
    summarize,
)

PROVIDER_ERROR_MESSAGE = "Summarization provider failed."
TIMEOUT_ERROR_MESSAGE = "Summarization exceeded the overall deadline."


class FakeClock:
    """A monotonic clock whose sleep operation advances time immediately."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class ControlledAgent:
    """Return configured structured outputs or raise configured exceptions."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = iter(outcomes)
        self.prompts: list[str] = []
        self.timeouts: list[float | None] = []

    @property
    def call_count(self) -> int:
        return len(self.prompts)

    def run_sync(self, prompt: str, *, timeout: float | None = None):
        self.prompts.append(prompt)
        self.timeouts.append(timeout)
        outcome = next(self._outcomes)
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(output=SimpleNamespace(summary=outcome))


def _install_controlled_agent(
    monkeypatch: pytest.MonkeyPatch,
    agent: ControlledAgent,
    clock: FakeClock,
) -> MagicMock:
    factory = MagicMock(return_value=agent)
    monkeypatch.setattr(summarizer_module, "_create_summary_agent", factory)
    monkeypatch.setattr(summarizer_module, "monotonic", clock.monotonic)
    monkeypatch.setattr(summarizer_module, "sleep", clock.sleep)
    return factory


def _advance_and_return(clock: FakeClock, seconds: float, outcome: object):
    def resolve() -> object:
        clock.advance(seconds)
        return outcome

    return resolve


def _assert_timeout(result: SummaryFailure) -> None:
    assert isinstance(result, SummaryFailure)
    assert result.outcome == "failure"
    assert result.error_category == "timeout"
    assert result.error_message == TIMEOUT_ERROR_MESSAGE


def test_public_interface_uses_only_explicit_configuration(monkeypatch):
    agent = ControlledAgent(["summary"])
    clock = FakeClock()
    factory = _install_controlled_agent(monkeypatch, agent, clock)
    model = object()
    model_settings = {"temperature": 0}

    for name in (
        "COMOURAT_MODEL",
        "COMOURAT_API_KEY",
        "COMOURAT_SYSTEM_PROMPT",
        "COMOURAT_RETRIES",
        "COMOURAT_TIMEOUT",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, "ignored-environment-value")

    def unexpected_configuration_access(*args, **kwargs):
        raise AssertionError("implicit configuration access")

    monkeypatch.setattr(os, "getenv", unexpected_configuration_access)
    monkeypatch.setattr("builtins.open", unexpected_configuration_access)

    result = summarize(
        model,
        "public content",
        system_prompt="explicit system prompt",
        model_settings=model_settings,
        retries=0,
        overall_timeout=12,
    )

    assert isinstance(result, SummarySuccess)
    factory.assert_called_once_with(
        model,
        "explicit system prompt",
        model_settings,
    )
    assert agent.prompts == ["public content"]
    assert agent.timeouts == [12]


def test_real_structured_agent_returns_a_public_success():
    model = TestModel(custom_output_args={"summary": "  from test model  "})

    result = summarize(model, "content", retries=0, overall_timeout=5)

    assert isinstance(result, SummarySuccess)
    assert result.summary == "from test model"


def test_nonempty_provider_success_is_trimmed(monkeypatch):
    agent = ControlledAgent([" \n  A tidy summary. \t "])
    _install_controlled_agent(monkeypatch, agent, FakeClock())

    result = summarize(object(), "content", retries=0)

    assert isinstance(result, SummarySuccess)
    assert result.outcome == "success"
    assert result.summary == "A tidy summary."
    assert agent.call_count == 1


@pytest.mark.parametrize("empty_text", ["", " \n\t "])
def test_empty_provider_success_can_be_allowed(monkeypatch, empty_text):
    agent = ControlledAgent([empty_text])
    _install_controlled_agent(monkeypatch, agent, FakeClock())

    result = summarize(
        object(),
        "content",
        require_non_empty=False,
        retries=4,
    )

    assert isinstance(result, SummarySuccess)
    assert result.outcome == "success"
    assert result.summary == ""
    assert agent.call_count == 1


@pytest.mark.parametrize("empty_text", ["", " \n\t "])
def test_empty_provider_success_can_be_rejected_without_retry(monkeypatch, empty_text):
    agent = ControlledAgent([empty_text])
    _install_controlled_agent(monkeypatch, agent, FakeClock())

    result = summarize(
        object(),
        "content",
        require_non_empty=True,
        retries=4,
    )

    assert isinstance(result, EmptySummary)
    assert result.outcome == "empty"
    assert result.summary == ""
    assert agent.call_count == 1


def test_success_after_failure_uses_only_remaining_time(monkeypatch):
    clock = FakeClock()
    agent = ControlledAgent(
        [
            _advance_and_return(clock, 4, RuntimeError("transient secret")),
            _advance_and_return(clock, 3, "  recovered summary  "),
        ]
    )
    _install_controlled_agent(monkeypatch, agent, clock)

    result = summarize(
        object(),
        "content",
        retries=1,
        retry_delay=2,
        overall_timeout=20,
    )

    assert isinstance(result, SummarySuccess)
    assert result.summary == "recovered summary"
    assert agent.call_count == 2
    assert agent.timeouts == [20, 14]
    assert clock.sleeps == [2]


@pytest.mark.parametrize(
    ("retries", "expected_calls"),
    [(0, 1), (2, 3)],
)
def test_retry_limit_returns_sanitized_provider_failure(
    monkeypatch, retries, expected_calls
):
    agent = ControlledAgent([RuntimeError("private")] * expected_calls)
    _install_controlled_agent(monkeypatch, agent, FakeClock())

    result = summarize(
        object(),
        "content",
        retries=retries,
        overall_timeout=1_000,
    )

    assert isinstance(result, SummaryFailure)
    assert result.outcome == "failure"
    assert result.error_category == "provider"
    assert result.error_message == PROVIDER_ERROR_MESSAGE
    assert agent.call_count == expected_calls


def test_deadline_before_first_attempt_makes_no_provider_call(monkeypatch):
    agent = ControlledAgent([])
    clock = FakeClock()
    _install_controlled_agent(monkeypatch, agent, clock)
    monotonic_values = iter([0.0, 10.0])
    monkeypatch.setattr(summarizer_module, "monotonic", lambda: next(monotonic_values))

    result = summarize(object(), "content", overall_timeout=10)

    _assert_timeout(result)
    assert agent.call_count == 0


def test_deadline_after_failure_prevents_a_later_attempt(monkeypatch):
    clock = FakeClock()
    agent = ControlledAgent([_advance_and_return(clock, 10, RuntimeError("private"))])
    _install_controlled_agent(monkeypatch, agent, clock)

    result = summarize(
        object(),
        "content",
        retries=5,
        overall_timeout=10,
    )

    _assert_timeout(result)
    assert agent.call_count == 1


def test_late_provider_result_cannot_become_success(monkeypatch):
    clock = FakeClock()
    agent = ControlledAgent([_advance_and_return(clock, 11, "too late")])
    _install_controlled_agent(monkeypatch, agent, clock)

    result = summarize(
        object(),
        "content",
        retries=5,
        overall_timeout=10,
    )

    _assert_timeout(result)
    assert agent.call_count == 1
    assert agent.timeouts == [10]


def test_agent_timeout_is_sanitized_and_not_retried(monkeypatch):
    agent = ControlledAgent([TimeoutError("secret provider timeout")])
    _install_controlled_agent(monkeypatch, agent, FakeClock())

    result = summarize(object(), "content", retries=5, overall_timeout=30)

    _assert_timeout(result)
    assert agent.call_count == 1


def test_retry_delay_is_capped_by_the_overall_deadline(monkeypatch):
    clock = FakeClock()
    agent = ControlledAgent([RuntimeError("private")])
    _install_controlled_agent(monkeypatch, agent, clock)

    result = summarize(
        object(),
        "content",
        retries=5,
        retry_delay=60,
        overall_timeout=5,
    )

    _assert_timeout(result)
    assert agent.call_count == 1
    assert clock.sleeps == [5]
    assert clock.now == 5


def test_retry_count_and_deadline_end_operations_independently(monkeypatch):
    short_clock = FakeClock()
    deadline_limited_agent = ControlledAgent([RuntimeError("private")])
    _install_controlled_agent(monkeypatch, deadline_limited_agent, short_clock)

    deadline_result = summarize(
        object(),
        "content",
        retries=100,
        retry_delay=20,
        overall_timeout=5,
    )

    _assert_timeout(deadline_result)
    assert deadline_limited_agent.call_count == 1
    assert short_clock.sleeps == [5]

    long_clock = FakeClock()
    retry_limited_agent = ControlledAgent([RuntimeError("private")] * 2)
    _install_controlled_agent(monkeypatch, retry_limited_agent, long_clock)

    retry_result = summarize(
        object(),
        "content",
        retries=1,
        overall_timeout=10_000,
    )

    assert isinstance(retry_result, SummaryFailure)
    assert retry_result.error_category == "provider"
    assert retry_limited_agent.call_count == 2


def test_public_failure_excludes_all_sensitive_provider_data(monkeypatch):
    sensitive_values = (
        "credential-secret",
        "authorization-header-secret",
        "secret-bearing prompt",
        "raw-provider-response",
    )
    provider_error = RuntimeError(" | ".join(sensitive_values))
    agent = ControlledAgent([provider_error])
    _install_controlled_agent(monkeypatch, agent, FakeClock())

    result = summarize(object(), sensitive_values[2], retries=0)

    assert isinstance(result, SummaryFailure)
    assert result.model_dump() == {
        "outcome": "failure",
        "error_category": "provider",
        "error_message": PROVIDER_ERROR_MESSAGE,
    }
    assert not hasattr(result, "exception")
    assert not hasattr(result, "raw_response")
    public_text = repr(result) + result.model_dump_json()
    for sensitive_value in sensitive_values:
        assert sensitive_value not in public_text


@pytest.mark.parametrize("retries", [-1, -100])
def test_negative_retry_count_is_rejected_before_agent_creation(monkeypatch, retries):
    agent = ControlledAgent([])
    factory = _install_controlled_agent(monkeypatch, agent, FakeClock())

    with pytest.raises(ValueError, match="retries"):
        summarize(object(), "content", retries=retries)

    factory.assert_not_called()
    assert agent.call_count == 0


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, -math.inf])
def test_invalid_overall_timeout_is_rejected_before_agent_creation(
    monkeypatch, timeout
):
    agent = ControlledAgent([])
    factory = _install_controlled_agent(monkeypatch, agent, FakeClock())

    with pytest.raises(ValueError, match="overall_timeout"):
        summarize(object(), "content", overall_timeout=timeout)

    factory.assert_not_called()
    assert agent.call_count == 0


@pytest.mark.parametrize("delay", [-1, math.nan, math.inf, -math.inf])
def test_invalid_retry_delay_is_rejected_before_agent_creation(monkeypatch, delay):
    agent = ControlledAgent([])
    factory = _install_controlled_agent(monkeypatch, agent, FakeClock())

    with pytest.raises(ValueError, match="retry_delay"):
        summarize(object(), "content", retry_delay=delay)

    factory.assert_not_called()
    assert agent.call_count == 0
