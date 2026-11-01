"""comourat collector test: arXiv core produces a neutral collection (AC3).

Mirrors the mocked-feed style of tests/test_collectors.py but exercises the
comourat core standalone, using the neutral PaperCandidate model.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from comourat.collectors.arxiv import (
    ArxivPaperCollectorCore,
    candidate_from_entry,
)
from comourat.collectors.arxiv_html_affiliations import ArxivHtmlAffiliationFetcherCore
from comourat.collectors.seed_expander import SeedExpanderCore
from comourat.models import (
    PaperCandidate,
    PaperCandidateCollection,
    PaperIdentity,
    PaperPage,
    PaperRecord,
)

SAMPLE_ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Test Paper One</title>
    <link href="http://arxiv.org/abs/2401.00001"/>
    <description>arXiv:2401.00001v1 [cs.AI] Test Paper One
Abstract: Abstract of paper one</description>
    <author><name>J. Smith</name></author>
    <published>2024-01-15T00:00:00Z</published>
  </entry>
</feed>
"""


class TestCandidateFromEntry:
    def test_candidate_carries_collection_metadata(self):
        entry = SimpleNamespace(
            title="A paper",
            link="http://arxiv.org/abs/2401.00001",
            description="arXiv:2401.00001v1 [cs.AI] A paper\nAbstract: text",
            authors=[{"name": "J. Smith"}],
            arxiv_announce_type="replace",
        )
        candidate = candidate_from_entry(entry)
        assert isinstance(candidate, PaperCandidate)
        assert candidate.arxiv_id == "2401.00001"
        assert candidate.authors == ["Smith"]
        assert candidate.provenance == ["arxiv_rss"]
        assert candidate.announce_type == "replace"
        assert candidate.affiliations is None


class TestDefaultModelCollectionMetadata:
    def test_affiliation_fetcher_updates_default_candidate(self):
        candidate = PaperCandidate(
            title="Paper", authors=["Alice"], arxiv_id="2401.00001"
        )
        atom_response = MagicMock()
        atom_response.text = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            "<published>2024-01-15T00:00:00Z</published>"
            "</entry></feed>"
        )
        html_response = MagicMock()
        html_response.text = (
            '<span class="ltx_personname">Alice</span>'
            '<span class="ltx_contact ltx_role_affiliation">'
            '<span class="ltx_contact_name">Affiliation: </span>University</span>'
        )
        http_client = MagicMock()
        http_client.get.side_effect = [atom_response, html_response]

        result, _ = ArxivHtmlAffiliationFetcherCore(
            http_client=http_client, request_delay_seconds=0
        ).fetch_affiliations(PaperCandidateCollection(papers=[candidate]))

        assert result.papers[0].publication_date == "2024-01-15"
        assert result.papers[0].affiliations == {"Alice": ["University"]}

    def test_seed_expander_records_all_generation_methods_on_default_model(self):
        record = PaperRecord(
            identity=PaperIdentity(arxiv_id="2401.00001"), title="Paper"
        )

        class GraphClient:
            def resolve_by_arxiv_id(self, arxiv_id):
                return record

            def resolve_by_doi(self, doi):
                return record

            def get_citations(self, identity, continuation=None):
                return PaperPage(papers=[record])

            def search_papers(self, query, continuation=None):
                return PaperPage(papers=[record])

            def get_references(self, identity, limit=None):
                return [record]

        candidates, _ = SeedExpanderCore(
            paper_graph_client=GraphClient(),
            forward_budget=1,
            search_budget=1,
            backward_budget=1,
        ).expand([SimpleNamespace(title="Seed", arxiv_id="2401.00002", doi=None)])

        assert len(candidates.papers) == 1
        assert candidates.papers[0].provenance == [
            "backward_references",
            "forward_citations",
            "relevance_search",
        ]


class TestArxivCoreNewestMode:
    def test_collect_returns_neutral_collection(self):
        response = MagicMock()
        response.text = SAMPLE_ARXIV_FEED
        http_client = MagicMock()
        http_client.get.return_value = response

        collector = ArxivPaperCollectorCore(
            http_client=http_client,
            api_url="http://example.com/feed",
            mode="newest",
        )
        result = collector.collect()
        assert isinstance(result, PaperCandidateCollection)
        assert len(result.papers) == 1
        assert result.papers[0].title == "Test Paper One"
        assert result.papers[0].description == "Abstract of paper one"
        assert result.papers[0].urls_seen == ["http://arxiv.org/abs/2401.00001"]

    def test_filter_new_submissions(self):
        papers = [
            PaperCandidate(
                title="A",
                authors=["X"],
                description="d",
                arxiv_id="2401.00001",
                announce_type="new",
            ),
            PaperCandidate(
                title="B",
                authors=["Y"],
                description="d",
                arxiv_id="2401.00002",
                announce_type="replace",
            ),
        ]
        collector = ArxivPaperCollectorCore.__new__(ArxivPaperCollectorCore)
        out, kept, excluded = collector.filter_new_submissions(
            PaperCandidateCollection(papers=papers)
        )
        assert (len(out.papers), kept, excluded) == (1, 1, 1)

    def test_most_relevant_requires_start_date(self):
        with pytest.raises(ValueError):
            ArxivPaperCollectorCore(
                http_client=MagicMock(),
                api_url="http://example.com",
                mode="most_relevant",
            )
