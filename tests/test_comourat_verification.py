"""Standalone tests for neutral arXiv PDF verification (spec 22, AC9)."""

from unittest.mock import MagicMock

from comourat.models import PaperRecord
from comourat.verification.arxiv_pdf import (
    ArxivPdfVerificationStatus,
    ArxivPdfVerifierCore,
)


def _paper(title="Attention Is All You Need"):
    return PaperRecord(title=title)


def _verifier(client=None, threshold=0.9):
    client = client or MagicMock()
    return (
        ArxivPdfVerifierCore(
            arxiv_client=client,
            title_similarity_threshold=threshold,
        ),
        client,
    )


class TestArxivPdfVerifierCore:
    def test_matching_title_returns_pdf_url(self):
        client = MagicMock()
        client.search_by_title.return_value = (
            "1706.03762",
            "Attention Is All You Need",
        )
        core, _ = _verifier(client)

        result = core.verify([_paper()])

        outcome = result.outcomes[0]
        assert outcome.status == ArxivPdfVerificationStatus.VERIFIED
        assert outcome.pdf_url == "https://arxiv.org/pdf/1706.03762"
        assert outcome.reason is None
        client.search_by_title.assert_called_once_with("Attention Is All You Need")

    def test_no_arxiv_result_has_distinct_outcome(self):
        client = MagicMock()
        client.search_by_title.return_value = None
        core, _ = _verifier(client)

        outcome = core.verify([_paper()]).outcomes[0]

        assert outcome.status == ArxivPdfVerificationStatus.NOT_FOUND
        assert outcome.pdf_url is None

    def test_title_mismatch_has_distinct_outcome(self):
        client = MagicMock()
        client.search_by_title.return_value = ("2401.99999", "Different title")
        core, _ = _verifier(client)

        outcome = core.verify([_paper()]).outcomes[0]

        assert outcome.status == ArxivPdfVerificationStatus.TITLE_MISMATCH
        assert outcome.pdf_url is None

    def test_request_error_has_distinct_outcome_and_continues(self):
        client = MagicMock()
        client.search_by_title.side_effect = [
            TimeoutError("request timed out"),
            ("1706.03762", "Attention Is All You Need"),
        ]
        core, _ = _verifier(client)

        result = core.verify([_paper(), _paper()])

        assert [o.status for o in result.outcomes] == [
            ArxivPdfVerificationStatus.API_ERROR,
            ArxivPdfVerificationStatus.VERIFIED,
        ]
        assert result.outcomes[0].pdf_url is None
        assert result.outcomes[1].pdf_url == "https://arxiv.org/pdf/1706.03762"

    def test_mixed_similarity_is_rejected_at_threshold(self):
        client = MagicMock()
        client.search_by_title.return_value = ("2401.00001", "Unrelated paper")
        core, _ = _verifier(client, threshold=1.0)

        outcome = core.verify([_paper()]).outcomes[0]

        assert outcome.status == ArxivPdfVerificationStatus.TITLE_MISMATCH

    def test_empty_input_returns_empty_outcomes(self):
        core, _ = _verifier()
        assert core.verify([]).outcomes == []
