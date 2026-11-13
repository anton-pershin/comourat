"""comourat model surface tests (AC4).

Checks collection metadata fields and excludes application-only research data.
"""

import pytest
from pydantic import TypeAdapter

from comourat.models import (
    EnrichmentResult,
    EnrichmentSuccess,
    HNPostCollection,
    HNPostInfo,
    PaperCandidate,
    PaperCandidateCollection,
    PaperIdentity,
    PaperInfo,
    PaperRecord,
    RedditPostCollection,
    RedditPostInfo,
)

APPLICATION_ONLY_FIELDS = {
    "research_context",
    "relevance_score",
}


class TestPaperCandidateBoundary:
    def test_influence_fields_present(self):
        fields = PaperCandidate.model_fields
        assert "influence_score" in fields

    def test_collection_metadata_fields_present(self):
        fields = PaperCandidate.model_fields
        assert {"provenance", "announce_type", "affiliations"} <= set(fields)

    def test_collection_metadata_defaults_and_roundtrip(self):
        candidate = PaperCandidate(title="T", authors=["A"])
        assert candidate.provenance == []
        assert candidate.announce_type is None
        assert candidate.affiliations is None

        candidate.provenance = ["forward_citations"]
        candidate.announce_type = "replace"
        candidate.affiliations = {"A": ["University"]}
        assert (
            PaperCandidate.model_validate_json(candidate.model_dump_json()) == candidate
        )

    def test_application_only_fields_absent(self):
        fields = set(PaperCandidate.model_fields)
        assert fields & APPLICATION_ONLY_FIELDS == set()

    def test_roundtrip_serialization(self):
        candidate = PaperCandidate(
            title="T",
            authors=["A"],
            description="D",
            urls_seen=["https://arxiv.org/abs/2401.00001"],
            arxiv_id="2401.00001",
            semantic_scholar_id="provider-paper-id",
            publication_date="2024-01-01",
            influence_score=77,
        )
        assert (
            PaperCandidate.model_validate_json(candidate.model_dump_json()) == candidate
        )

    def test_collection_roundtrip(self):
        collection = PaperCandidateCollection(
            papers=[
                PaperCandidate(title="T", authors=["A"], description="D"),
            ]
        )
        assert (
            PaperCandidateCollection.model_validate_json(collection.model_dump_json())
            == collection
        )


class TestPostModels:
    def test_reddit_and_hn_models_roundtrip(self):
        reddit = RedditPostInfo(
            subreddit="r",
            submission_id="abc",
            title="t",
            author="a",
            date="2024-01-01T00:00:00Z",
            url="https://e.com",
            score=5,
        )
        assert RedditPostCollection.model_validate_json(
            RedditPostCollection(posts=[reddit]).model_dump_json()
        ).posts == [reddit]
        hn = HNPostInfo(
            hn_id="1",
            title="t",
            author="a",
            date="2024-01-01T00:00:00Z",
            url="https://e.com",
            points=3,
        )
        assert HNPostCollection.model_validate_json(
            HNPostCollection(posts=[hn]).model_dump_json()
        ).posts == [hn]


class TestIdentityModels:
    def test_identity_from_values_normalizes(self):
        identity = PaperIdentity.from_values(arxiv_id="arXiv:2401.00001v2")
        assert identity.arxiv_id == "2401.00001"
        assert identity.doi is None

    def test_empty_identity_allowed_no_error(self):
        # from_values with neither id is accepted; callers decide whether an
        # empty identity is an error.
        identity = PaperIdentity.from_values()
        assert identity.arxiv_id is None
        assert identity.doi is None

    def test_paper_record_carries_influence_fields(self):
        record = PaperRecord(
            identity=PaperIdentity(arxiv_id="2401.00001"),
            title="T",
            authors=[],
            citation_count=7,
            raw_influence={"fwci": 1.5},
        )
        assert record.citation_count == 7
        assert record.raw_influence == {"fwci": 1.5}


class TestInfoAndEnrichmentModels:
    def test_paper_info_and_enrichment_roundtrip(self):
        info = PaperInfo(
            title="T", link="https://arxiv.org/abs/2401.00001", abstract="A"
        )
        assert info.author_ids is None
        assert PaperInfo.model_validate_json(info.model_dump_json()) == info
        enrichment = EnrichmentSuccess(additional_context=["c1"])
        adapter = TypeAdapter(EnrichmentResult)
        assert adapter.validate_json(enrichment.model_dump_json()) == enrichment

    def test_author_record_roundtrip(self):
        from comourat.models import AuthorRecord

        author = AuthorRecord(
            author_id="author-1",
            name="Author",
            affiliations=["University"],
        )

        assert AuthorRecord.model_validate_json(author.model_dump_json()) == author
