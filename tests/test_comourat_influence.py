"""comourat influence tests: post, citation and authority cores (AC4).

Mirrors mourat's influence tests but exercises the comourat cores directly,
without any pipeline machinery.
"""

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from comourat.influence.authority import (
    AuthorityInfluenceAssessorCore,
    AuthorityResult,
    AuthorityVerdict,
)
from comourat.influence.citation import (
    MEASURE_CITATIONS_PER_YEAR,
    MEASURE_FWCI,
    CitationInfluenceAssessor,
    _interpolate,
    _years_since,
)
from comourat.influence.post import compute_influence_score
from comourat.models import PaperCandidate, PaperCandidateCollection


class TestComputeInfluenceScore:
    def test_anchor_values(self):
        assert compute_influence_score(0, reference=100) == 0
        assert compute_influence_score(100, reference=100) == 50
        assert compute_influence_score(10_000, reference=100) == 99

    def test_reference_below_one_clamped(self):
        # reference 0 is clamped to 1; ratio 5 -> 100*5/6 rounds to 83
        assert compute_influence_score(5, reference=0) == 83


class TestCitationInterpolation:
    def test_saturates_at_both_ends(self):
        breakpoints = [(0.0, 0), (2.0, 50), (10.0, 100)]
        assert _interpolate(-1.0, breakpoints) == 0.0
        assert _interpolate(99.0, breakpoints) == 100.0

    def test_interpolates_between_breakpoints(self):
        breakpoints = [(0.0, 0), (2.0, 50), (10.0, 100)]
        assert _interpolate(1.0, breakpoints) == 25.0

    def test_years_since_uses_supplied_today(self):
        assert (
            _years_since("2020-06-01", today=__import__("datetime").date(2026, 6, 1))
            == 6
        )

    def test_years_since_undatable(self):
        assert _years_since(None) is None
        assert _years_since("not-a-date") is None


class TestCitationInfluenceAssessor:
    def _assessor(self):
        return CitationInfluenceAssessor(
            fwci_breakpoints=[(0.0, 0), (2.0, 50), (10.0, 100)],
            citations_per_year_breakpoints=[(0.0, 0), (10.0, 50), (100.0, 100)],
        )

    def test_fwci_preferred_when_present(self):
        paper = SimpleNamespace(
            influence_fwci=2.0,
            influence_cited_by_count=999,
            publication_date="2020-01-01",
        )
        score, measure = self._assessor()._assess_one(paper, today=None)
        assert score == 50
        assert measure == MEASURE_FWCI

    def test_falls_back_to_citations_per_year(self):
        paper = SimpleNamespace(
            influence_fwci=None,
            influence_cited_by_count=90,
            publication_date="2024-01-01",
        )
        score, measure = self._assessor()._assess_one(
            paper, today=__import__("datetime").date(2026, 1, 1)
        )
        assert measure == MEASURE_CITATIONS_PER_YEAR
        assert score == round(_interpolate(30.0, [(0.0, 0), (10.0, 50), (100.0, 100)]))

    def test_unmeasurable_returns_none_pair(self):
        paper = SimpleNamespace(
            influence_fwci=None,
            influence_cited_by_count=None,
            publication_date=None,
        )
        assert self._assessor()._assess_one(paper, today=None) == (None, None)


class TestAuthorityCore:
    def _core(self, agent_output=None, fail=False):
        core = AuthorityInfluenceAssessorCore.__new__(AuthorityInfluenceAssessorCore)
        agent = MagicMock()
        if fail:
            agent.run_sync.side_effect = RuntimeError("transport down")
        else:
            agent.run_sync.return_value = MagicMock(output=agent_output)
        core.agent = agent
        core.logger = MagicMock()
        core.batch_size = 2
        core.request_retries = 0
        core.request_retry_delay_seconds = 0.0
        core.time = MagicMock()
        return core

    def test_every_candidate_gets_a_score(self):
        verdicts = [AuthorityVerdict(id="paper_0", score=42)]
        core = self._core(AuthorityResult(verdicts=verdicts))
        collection = PaperCandidateCollection(
            papers=[PaperCandidate(title="T", authors=["A"], description="D")]
        )
        output, lines = core.assess(collection)
        assert output.papers[0].influence_score == 42
        assert lines

    def test_transport_failure_zero_scores(self):
        core = self._core(fail=True)
        collection = PaperCandidateCollection(
            papers=[PaperCandidate(title="T", authors=["A"], description="D")]
        )
        output, _ = core.assess(collection)
        assert output.papers[0].influence_score == 0

    def test_follow_up_call_for_missing_verdicts(self):
        first = AuthorityResult(verdicts=[AuthorityVerdict(id="paper_0", score=10)])
        follow_up = AuthorityResult(verdicts=[AuthorityVerdict(id="paper_1", score=20)])
        core = self._core(first)
        core.agent.run_sync.side_effect = [
            MagicMock(output=first),
            MagicMock(output=follow_up),
        ]
        collection = PaperCandidateCollection(
            papers=[
                PaperCandidate(title="T0", authors=["A"], description="D"),
                PaperCandidate(title="T1", authors=["A"], description="D"),
            ]
        )
        output, _ = core.assess(collection)
        assert [p.influence_score for p in output.papers] == [10, 20]
        assert core.agent.run_sync.call_count == 2

    def test_affiliations_reach_the_prompt(self):
        # Review fix: affiliations must arrive via the explicit parameter,
        # not a getattr on the neutral model.
        verdicts = [
            AuthorityVerdict(id="paper_0", score=70),
            AuthorityVerdict(id="paper_1", score=10),
        ]
        core = self._core(AuthorityResult(verdicts=verdicts))
        collection = PaperCandidateCollection(
            papers=[
                PaperCandidate(title="T0", authors=["Alice"], description="D"),
                PaperCandidate(title="T1", authors=["Bob"], description="D"),
            ]
        )
        output, _ = core.assess(
            collection,
            affiliations={0: {"Alice": ["MIT"]}},
        )
        prompt = core.agent.run_sync.call_args[0][0]
        assert "MIT" in prompt
        assert "unknown" in prompt  # paper 1 has no affiliations
        assert [p.influence_score for p in output.papers] == [70, 10]

    def test_no_affiliations_given_renders_unknown(self):
        verdicts = [AuthorityVerdict(id="paper_0", score=5)]
        core = self._core(AuthorityResult(verdicts=verdicts))
        collection = PaperCandidateCollection(
            papers=[PaperCandidate(title="T", authors=["A"], description="D")]
        )
        core.assess(collection)
        prompt = core.agent.run_sync.call_args[0][0]
        assert '"affiliations": "unknown"' in prompt


class TestAuthorityStandaloneRetry:
    """Direct core tests, no adapter, no injected time/logger (review 2)."""

    def test_exhausted_retries_fail_open_without_error(self):
        agent = MagicMock()
        agent.run_sync.side_effect = RuntimeError("transport down")
        core = AuthorityInfluenceAssessorCore.__new__(AuthorityInfluenceAssessorCore)
        core.agent = agent
        core.logger = logging.getLogger("test.authority")
        core.time = time  # the DEFAULT core wiring, nothing injected
        core.batch_size = 2
        core.request_retries = 2
        core.request_retry_delay_seconds = 0.0
        collection = PaperCandidateCollection(
            papers=[PaperCandidate(title="T", authors=["A"], description="D")]
        )
        output, _ = core.assess(collection)
        assert output.papers[0].influence_score == 0
        assert agent.run_sync.call_count == 3  # 1 + request_retries

    def test_constructed_core_retries_and_sleeps(self):
        # The real constructor path: time/logger initialized inside the core.
        from pydantic_ai.models.function import FunctionModel

        def failing(messages, tools):
            raise RuntimeError("transport down")

        core = AuthorityInfluenceAssessorCore(
            model=FunctionModel(failing),
            request_retries=1,
            request_retry_delay_seconds=0.0,
        )
        collection = PaperCandidateCollection(
            papers=[PaperCandidate(title="T", authors=["A"], description="D")]
        )
        output, _ = core.assess(collection)
        assert output.papers[0].influence_score == 0
