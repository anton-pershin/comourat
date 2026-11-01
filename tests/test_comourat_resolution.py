"""Standalone tests for comourat paper candidate resolution (AC8)."""

from unittest.mock import MagicMock

from comourat.models import PaperCandidate, PaperIdentity, PaperPage, PaperRecord
from comourat.resolution.paper_resolver import PaperResolutionStatus, PaperResolverCore


def _candidate(**overrides):
    values = {
        "title": "Attention Is All You Need",
        "authors": ["Vaswani"],
        "description": "Transformer paper",
        "urls_seen": [],
    }
    values.update(overrides)
    return PaperCandidate(**values)


def _record(**overrides):
    values = {
        "identity": PaperIdentity.from_values(doi="10.5555/3294995"),
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani"],
        "abstract": "Attention is all",
    }
    values.update(overrides)
    return PaperRecord(**values)


def _resolver(client=None, threshold=0.9):
    client = client or MagicMock()
    return (
        PaperResolverCore(
            paper_graph_client=client,
            title_similarity_threshold=threshold,
        ),
        client,
    )


class TestPaperResolverCore:
    def test_resolves_by_arxiv_id_and_returns_neutral_record(self):
        client = MagicMock()
        record = _record(identity=PaperIdentity.from_values(arxiv_id="1706.03762"))
        client.resolve_by_arxiv_id.return_value = record
        core, _ = _resolver(client)

        result = core.resolve([_candidate(arxiv_id="1706.03762")])

        assert len(result.resolutions) == 1
        resolution = result.resolutions[0]
        assert resolution.status == PaperResolutionStatus.RESOLVED
        assert resolution.record == record
        assert resolution.reason is None
        client.resolve_by_arxiv_id.assert_called_once_with("1706.03762")
        client.search_papers.assert_not_called()

    def test_extracts_arxiv_id_from_url(self):
        client = MagicMock()
        client.resolve_by_arxiv_id.return_value = _record(
            identity=PaperIdentity.from_values(arxiv_id="2401.00001")
        )
        core, _ = _resolver(client)

        result = core.resolve(
            [_candidate(urls_seen=["https://arxiv.org/pdf/2401.00001v2"])]
        )

        assert result.resolutions[0].status == PaperResolutionStatus.RESOLVED
        client.resolve_by_arxiv_id.assert_called_once_with("2401.00001")

    def test_uses_doi_after_arxiv_lookup_fails(self):
        client = MagicMock()
        client.resolve_by_arxiv_id.return_value = None
        record = _record()
        client.resolve_by_doi.return_value = record
        core, _ = _resolver(client)

        result = core.resolve([_candidate(arxiv_id="1706.03762", doi="10.5555/x")])

        assert result.resolutions[0].record == record
        client.resolve_by_doi.assert_called_once_with("10.5555/x")
        client.search_papers.assert_not_called()

    def test_falls_back_to_title_search_when_identity_not_found(self):
        client = MagicMock()
        client.resolve_by_arxiv_id.return_value = None
        record = _record()
        client.search_papers.return_value = PaperPage(papers=[record])
        core, _ = _resolver(client)

        result = core.resolve([_candidate(arxiv_id="1706.03762")])

        assert result.resolutions[0].status == PaperResolutionStatus.RESOLVED
        assert result.resolutions[0].record == record
        client.search_papers.assert_called_once_with("Attention Is All You Need")

    def test_rejects_record_below_title_similarity_threshold(self):
        client = MagicMock()
        client.search_papers.return_value = PaperPage(
            papers=[_record(title="An unrelated paper")]
        )
        core, _ = _resolver(client, threshold=0.9)

        result = core.resolve([_candidate()])

        resolution = result.resolutions[0]
        assert resolution.status == PaperResolutionStatus.UNRESOLVED
        assert resolution.record is None
        assert resolution.reason == "provider_title_mismatch"

    def test_reports_candidate_without_any_matching_record(self):
        client = MagicMock()
        client.search_papers.return_value = PaperPage(papers=[])
        core, _ = _resolver(client)

        result = core.resolve([_candidate()])

        resolution = result.resolutions[0]
        assert resolution.status == PaperResolutionStatus.UNRESOLVED
        assert resolution.record is None
        assert resolution.reason == "no_paper_found"

    def test_provider_exception_becomes_api_error_outcome(self):
        client = MagicMock()
        client.search_papers.side_effect = RuntimeError("provider unavailable")
        core, _ = _resolver(client)

        result = core.resolve([_candidate()])

        resolution = result.resolutions[0]
        assert resolution.status == PaperResolutionStatus.UNRESOLVED
        assert resolution.reason == "api_error"

    def test_empty_input_returns_empty_result(self):
        core, _ = _resolver()
        assert core.resolve([]).resolutions == []
