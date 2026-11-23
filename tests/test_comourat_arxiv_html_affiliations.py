"""Controlled arXiv HTML affiliation layout tests for issue #32."""

from unittest.mock import MagicMock

from comourat.collectors.arxiv_html_affiliations import (
    ArxivHtmlAffiliationFetcherCore,
    _extract_affiliations,
)
from comourat.models import PaperCandidate, PaperCandidateCollection

NUMBERED_NOTES_HTML = """
<article class="ltx_document">
<p class="ltx_p ltx_align_center">
<span class="ltx_text ltx_font_bold">Faraz Tahmasebi</span>
<sup class="ltx_sup">1</sup>
<span class="ltx_text ltx_font_bold">Michael Pellauer</span>
<sup class="ltx_sup">2</sup>
<span class="ltx_text ltx_font_bold">Hyoukjun Kwon</span>
<sup class="ltx_sup">1</sup>
<span class="ltx_text ltx_font_bold">Sitao Huang</span>
<sup class="ltx_sup">1</sup>
</p>
<span class="ltx_note ltx_role_footnotetext">
<sup class="ltx_note_mark">†</sup>
<span class="ltx_note_outer"><span class="ltx_note_content">
<sup class="ltx_note_mark">†</sup>
<span class="ltx_note_type">footnotetext: </span>
<sup class="ltx_sup">1</sup>Department of Computer Science, University of
California, Irvine, CA, USA
<sup class="ltx_sup">2</sup>NVIDIA, USA.
Correspondence to: Faraz Tahmasebi &lt;tahmasef@uci.edu&gt;.
</span></span></span>
<div class="ltx_abstract">Abstract</div>
</article>
"""


TABULAR_AUTHORS_HTML = """
<article class="ltx_document ltx_authors_1line">
<div class="ltx_authors"><span class="ltx_creator ltx_role_author">
<span class="ltx_personname"><span class="ltx_tabular ltx_align_top">
<span class="ltx_tbody">
<span class="ltx_tr">
<span class="ltx_td"><span class="ltx_p"><span class="ltx_text ltx_font_bold">Anna Kuzina</span></span></span>
<span class="ltx_td"><span class="ltx_p"><span class="ltx_text ltx_font_bold">Mart van Baalen</span></span></span>
</span>
<span class="ltx_tr">
<span class="ltx_td"><span class="ltx_p">Qualcomm AI Research<sup class="ltx_sup">‡</sup></span></span>
<span class="ltx_td"><span class="ltx_p">Qualcomm AI Research</span></span>
</span>
<span class="ltx_tr">
<span class="ltx_td"><span class="ltx_p"><span class="ltx_text ltx_font_typewriter">akuzina@qti.qualcomm.com</span></span></span>
<span class="ltx_td"><span class="ltx_p"><span class="ltx_text ltx_font_typewriter">mart@qti.qualcomm.com</span></span></span>
</span>
<span class="ltx_tr">
<span class="ltx_td"><span class="ltx_p"><span class="ltx_text ltx_font_bold">Paul N. Whatmough</span></span></span>
<span class="ltx_td"><span class="ltx_p"><span class="ltx_text ltx_font_bold">Babak Ehteshami Bejnordi</span></span></span>
</span>
<span class="ltx_tr">
<span class="ltx_td"><span class="ltx_p">Qualcomm AI Research</span></span>
<span class="ltx_td"><span class="ltx_p">NVIDIA</span></span>
</span>
<span class="ltx_tr">
<span class="ltx_td"><span class="ltx_p"><span class="ltx_text ltx_font_typewriter">pwhatmou@qti.qualcomm.com</span></span></span>
<span class="ltx_td"><span class="ltx_p"><span class="ltx_text ltx_font_typewriter">babakint@gmail.com</span></span></span>
</span>
</span></span></span></span></div>
<span class="ltx_note ltx_role_footnotetext"><span class="ltx_note_content">
<sup class="ltx_note_mark">3</sup>Qualcomm AI Research is an initiative of Qualcomm Technologies, Inc.
</span></span>
</article>
"""


def _fetch_affiliations(html: str):
    candidate = PaperCandidate(
        title="Controlled paper", authors=["Alice"], arxiv_id="2401.00001"
    )
    atom_response = MagicMock()
    atom_response.text = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<published>2024-01-15T00:00:00Z</published>"
        "</entry></feed>"
    )
    html_response = MagicMock()
    html_response.text = html
    http_client = MagicMock()
    http_client.get.side_effect = [atom_response, html_response]

    return ArxivHtmlAffiliationFetcherCore(
        http_client=http_client, request_delay_seconds=0
    ).fetch_affiliations(PaperCandidateCollection(papers=[candidate]))


def test_inline_affiliations_keep_order_and_remove_exact_duplicates():
    html = """
    <span class="ltx_personname">Alice</span>
    <span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">
    Affiliation: </span>University of Somewhere</span>
    <span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">
    Affiliation: </span>Research Lab</span>
    <span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">
    Affiliation: </span>Research Lab</span>
    <span class="ltx_personname">Bob</span>
    <span class="ltx_contact ltx_role_affiliation"><span class="ltx_contact_name">
    Affiliation: </span>Other Institute</span>
    """

    assert _extract_affiliations(html) == {
        "Alice": ["University of Somewhere", "Research Lab"],
        "Bob": ["Other Institute"],
    }


def test_numbered_note_affiliations_map_shared_and_distinct_references():
    affiliations = _extract_affiliations(NUMBERED_NOTES_HTML)

    assert list(affiliations) == [
        "Faraz Tahmasebi",
        "Michael Pellauer",
        "Hyoukjun Kwon",
        "Sitao Huang",
    ]
    california = (
        "Department of Computer Science, University of California, Irvine, CA, USA"
    )
    assert affiliations == {
        "Faraz Tahmasebi": [california],
        "Michael Pellauer": ["NVIDIA, USA."],
        "Hyoukjun Kwon": [california],
        "Sitao Huang": [california],
    }
    assert all(
        "Correspondence" not in affiliation
        for values in affiliations.values()
        for affiliation in values
    )


def test_tabular_affiliations_align_by_column_and_skip_emails_and_notes():
    assert _extract_affiliations(TABULAR_AUTHORS_HTML) == {
        "Anna Kuzina": ["Qualcomm AI Research"],
        "Mart van Baalen": ["Qualcomm AI Research"],
        "Paul N. Whatmough": ["Qualcomm AI Research"],
        "Babak Ehteshami Bejnordi": ["NVIDIA"],
    }


def test_all_empty_affiliation_lists_are_unknown():
    result, lines = _fetch_affiliations('<span class="ltx_personname">Alice</span>')

    assert result.papers[0].affiliations is None
    assert result.papers[0].publication_date == "2024-01-15"
    assert "with affiliations: 0" in lines[0]
    assert "- Controlled paper" in lines


def test_partial_affiliations_retain_empty_author_lists():
    result, lines = _fetch_affiliations(
        '<span class="ltx_personname">Alice</span>'
        '<span class="ltx_contact ltx_role_affiliation">'
        '<span class="ltx_contact_name">Affiliation: </span>University</span>'
        '<span class="ltx_personname">Bob</span>'
    )

    assert result.papers[0].affiliations == {"Alice": ["University"], "Bob": []}
    assert "with affiliations: 1" in lines[0]
    assert "- Controlled paper" not in lines
