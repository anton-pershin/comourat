"""Citation influence: normalised influence onto the 0-100 schema range.

fwci (the provider's field-weighted,
age-normalised measure) is preferred; citations per year is the fallback when
fwci is absent or malformed. The chosen measure is recorded on the paper and
mapped onto 0-100 through the configured breakpoints of the same measure, so
two stored scores are comparable regardless of which measure produced them.
"""

import logging
import time
from datetime import date

logger = logging.getLogger(__name__)

MEASURE_FWCI = "fwci"
MEASURE_CITATIONS_PER_YEAR = "citations_per_year"


def _interpolate(value: float, breakpoints: list[tuple[float, int]]) -> float:
    """Piecewise-linear interpolation with saturation at both ends.

    `breakpoints` is a list of (measure_value, score) pairs sorted ascending;
    the ends saturate: values below the first breakpoint map to its score,
    values above the last map to the last score.
    """
    if not breakpoints:
        return 0.0
    if value <= breakpoints[0][0]:
        return float(breakpoints[0][1])
    if value >= breakpoints[-1][0]:
        return float(breakpoints[-1][1])
    for (x0, y0), (x1, y1) in zip(breakpoints, breakpoints[1:]):
        if value <= x1:
            fraction = (value - x0) / (x1 - x0)
            return y0 + fraction * (y1 - y0)
    return float(breakpoints[-1][1])


def _years_since(publication_date: str | None, today: date | None = None) -> int | None:
    """Whole years between publication and now; None when undatable."""
    if not publication_date:
        return None
    try:
        published = date.fromisoformat(publication_date)
    except ValueError:
        return None
    now = today or date.today()
    years = now.year - published.year
    if (now.month, now.day) < (published.month, published.day):
        years -= 1
    return max(years, 0)


class CitationInfluenceAssessor:
    """Assesses field- and age-normalised influence for resolved papers.

    Pass-through by count: every resolved paper leaves with an
    `influence_score` in 0-100 and `influence_measure_used` set — or, when no
    measure is computable, both stay None with a warning (a paper the resolver
    accepted but that carries no citation signal at all).
    """

    def __init__(
        self,
        fwci_breakpoints: list[tuple[float, int]],
        citations_per_year_breakpoints: list[tuple[float, int]],
    ) -> None:
        self.fwci_breakpoints: list[tuple[float, int]] = [
            (float(v), int(s)) for v, s in fwci_breakpoints
        ]
        self.citations_per_year_breakpoints: list[tuple[float, int]] = [
            (float(v), int(s)) for v, s in citations_per_year_breakpoints
        ]

    def _assess_one(self, item, today: date | None) -> tuple[int | None, str | None]:
        """One paper's (score, measure_used); (None, None) when unmeasurable.

        fwci is read from the resolved record's in-flight influence fields
        populated by the resolver; citations per year is computed from
        cited_by_count / years since publication.
        """
        fwci = getattr(item, "influence_fwci", None)
        if isinstance(fwci, (int, float)) and fwci == fwci:  # rules out NaN
            return (
                round(_interpolate(float(fwci), self.fwci_breakpoints)),
                MEASURE_FWCI,
            )
        citations = getattr(item, "influence_cited_by_count", None)
        years = _years_since(item.publication_date, today)
        if (
            isinstance(citations, (int, float))
            and citations >= 0
            and years is not None
            and years >= 0
        ):
            per_year = float(citations) / (years + 1)  # +1 avoids /0 on same-year
            return (
                round(_interpolate(per_year, self.citations_per_year_breakpoints)),
                MEASURE_CITATIONS_PER_YEAR,
            )
        return None, None

    def assess(self, papers, today: date | None = None) -> tuple[list, list[str]]:
        """Assess papers in place; returns (papers, summary_lines).

        `papers` is any list of objects carrying influence_fwci,
        influence_cited_by_count, publication_date, title, and writable
        influence_score / influence_measure_used attributes (duck-typed, so
        both comourat candidates and mourat resolved papers work).
        `today` overrides the current date (constructor-free clock injection
        for tests and replays); defaults to the system date.
        """
        t_start = time.monotonic()
        today = today or date.today()
        assessed = 0
        measure_counts = {MEASURE_FWCI: 0, MEASURE_CITATIONS_PER_YEAR: 0}
        unmeasurable: list[str] = []

        papers = list(papers)
        for item in papers:
            score, measure = self._assess_one(item, today)
            if score is None or measure is None:
                logger.warning(
                    "no influence measure computable for '%s' (fwci malformed, "
                    "citations or publication date absent)",
                    item.title,
                )
                unmeasurable.append(item.title)
                continue
            item.influence_score = score
            item.influence_measure_used = measure
            measure_counts[measure] += 1
            assessed += 1

        lines = [
            f"Papers in: {len(papers)}, assessed: {assessed}, "
            f"unmeasurable: {len(unmeasurable)}",
            f"Measures used: fwci={measure_counts[MEASURE_FWCI]}, "
            f"citations_per_year={measure_counts[MEASURE_CITATIONS_PER_YEAR]}",
        ]
        for title in unmeasurable:
            lines.append(f"### UNMEASURABLE: {title}\n")
        logger.debug(
            "assessed %d/%d papers | %.2fs",
            assessed,
            len(papers),
            time.monotonic() - t_start,
        )
        return papers, lines
