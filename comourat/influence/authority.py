"""Authority influence assessment.

Maps (authors, affiliations) -> a single 0-100 authority influence score
per paper via a batched LLM call, judged against prompt-configured anchors.

Batched classification per the established pattern: each paper in the
prompt carries a stable id; verdicts are matched back by id; hallucinated
ids are silently discarded. Fail-open with score 0: this score is not a
filter anywhere on the path, so a paper never receives a score that removes
it from the pipeline.

Verdict failure handling:
- (1) transport error / (2) malformed response on the whole batch: bounded
  retry loop (same batch re-sent up to `request_retries` attempts with
  `request_retry_delay_seconds` between attempts); after exhaustion every
  paper in the batch continues with score 0 and a logged warning.
- (3) verdict missing for a paper inside a valid response: ONE follow-up
  call containing only the missing papers (same retry loop); if that also
  fails, those papers continue with score 0 and a warning.
- (4) verdict for an id not in the batch: silently discarded.
- "LLM recognises nobody": score 0, not None.
"""

import hashlib
import json
import logging
import time

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from comourat.models import PaperCandidate, PaperCandidateCollection

DEFAULT_SYSTEM_PROMPT = (
    "You assess the authority influence of research papers. For each paper "
    "you judge the reputation of its authors and their institutional "
    "affiliations and output one integer score 0-100.\n"
    "Anchor points:\n"
    "- 100: the paper has a field-leading figure (a name any researcher in "
    "the field would recognise) or authors from a top-tier institution "
    "(e.g. a top-10 CS university, a famous industrial lab).\n"
    "- 50: active researchers from a reputable but not famous group.\n"
    "- 0: none of the authors or institutions is recognisable to you.\n"
    "Judge the authors and affiliations only; the paper's topic must not "
    "affect the score."
)


class AuthorityVerdict(BaseModel):
    id: str = Field(description="The paper id exactly as given in the input")
    score: int = Field(description="Authority influence score 0-100", ge=0, le=100)


class AuthorityResult(BaseModel):
    verdicts: list[AuthorityVerdict]


def _stable_identifier(paper: PaperCandidate) -> str:
    """Return a source id, or a stable content-derived id when none exists."""
    source_id = (
        paper.arxiv_id or paper.doi or (paper.urls_seen[0] if paper.urls_seen else None)
    )
    if source_id:
        return source_id
    content = json.dumps(
        [paper.title, paper.authors], ensure_ascii=False, separators=(",", ":")
    )
    return f"content-sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _render_papers(
    papers: list[tuple[str, PaperCandidate]],
    affiliations_by_id: dict[str, dict[str, list[str]]],
) -> str:
    lines = []
    for pid, paper in papers:
        affiliations = affiliations_by_id.get(pid)
        if affiliations:
            affs = "; ".join(
                f"{a}: {', '.join(insts)}" for a, insts in affiliations.items()
            )
        else:
            affs = "unknown"
        lines.append(
            f'{{"id": "{pid}", "title": {paper.title!r}, '
            f'"authors": {paper.authors!r}, "affiliations": "{affs}"}}'
        )
    return "\n".join(lines)


class AuthorityInfluenceAssessorCore:
    """Writes an influence_score onto every candidate.

        Pass-through by count: every candidate is returned with
        `influence_score` filled (0 when unmeasurable or unassessable). Which
    measure attribution is left to the consuming application.
    """

    def __init__(
        self,
        model: Model,
        batch_size: int = 25,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        request_retries: int = 2,
        request_retry_delay_seconds: float = 2.0,
    ) -> None:
        self.agent = Agent(
            model,
            output_type=AuthorityResult,
            system_prompt=system_prompt,
        )
        self.logger = logging.getLogger(__name__)
        self.time = time  # injectable patch point (tests may replace it)
        self.batch_size = batch_size
        self.request_retries = request_retries
        self.request_retry_delay_seconds = request_retry_delay_seconds

    def _call_with_retries(self, prompt: str, n_papers: int) -> AuthorityResult | None:
        """Bounded retry loop over one batch prompt; None on exhaustion."""
        for attempt in range(self.request_retries + 1):
            try:
                result: AuthorityResult = self.agent.run_sync(prompt).output
                return result
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if attempt >= self.request_retries:
                    self.logger.warning(
                        "authority batch of %d papers failed after %d attempts: %s",
                        n_papers,
                        attempt + 1,
                        exc,
                    )
                    return None
                self.logger.warning(
                    "authority batch attempt %d/%d failed: %s",
                    attempt + 1,
                    self.request_retries + 1,
                    exc,
                )
                self.time.sleep(self.request_retry_delay_seconds)
        return None

    def _run_batch(
        self,
        papers: list[tuple[str, PaperCandidate]],
        affiliations_by_id: dict[str, dict[str, list[str]]],
    ) -> tuple[dict[str, int], bool]:
        """One batch; return scores and whether a valid response was received."""
        prompt = (
            "Rate each paper's authority influence on a 0-100 scale.\n\n"
            + _render_papers(papers, affiliations_by_id)
            + "\n\nReturn one verdict per paper: its id (exactly as given) "
            "and a 'score' 0-100. Return nothing else."
        )
        result = self._call_with_retries(prompt, len(papers))
        if result is None:
            return {}, False
        valid_ids = {pid for pid, _ in papers}
        return {v.id: v.score for v in result.verdicts if v.id in valid_ids}, True

    def assess(
        self,
        data: PaperCandidateCollection,
        affiliations: dict[int, dict[str, list[str]]] | None = None,
    ) -> tuple[PaperCandidateCollection, list[str]]:
        """Assess papers in place; returns (papers, summary_lines).

        `affiliations` maps paper INDEX to author->affiliations for that
        paper (application-level data the neutral model cannot carry).
        Papers without an entry render as 'unknown' in the prompt.
        """
        affiliations = affiliations or {}
        t_start = time.monotonic()
        zero_scored: list[str] = []
        paper_assessments: list[str] = []
        total = len(data.papers)
        triples = [(f"paper_{i}", p) for i, p in enumerate(data.papers)]

        n_batches = (len(triples) + self.batch_size - 1) // self.batch_size
        for batch_idx, start in enumerate(range(0, len(triples), self.batch_size), 1):
            batch = triples[start : start + self.batch_size]
            self.logger.info(
                "authority batch %d/%d | papers %d-%d of %d | first: '%s'",
                batch_idx,
                n_batches,
                start + 1,
                start + len(batch),
                len(triples),
                batch[0][1].title[:60],
            )
            affs_by_id = {
                pid: affiliations[int(pid.removeprefix("paper_"))]
                for pid, _ in batch
                if int(pid.removeprefix("paper_")) in affiliations
            }
            scores, batch_responded = self._run_batch(batch, affs_by_id)

            missing = [(pid, p) for pid, p in batch if pid not in scores]
            unassessable_reasons = {
                pid: (
                    "missing verdict"
                    if batch_responded
                    else "batch request failed after retries"
                )
                for pid, _ in missing
            }
            if missing and scores:
                # ONE follow-up call with only the missing papers.
                self.logger.info(
                    "authority follow-up call for %d missing verdicts",
                    len(missing),
                )
                follow_up, follow_up_responded = self._run_batch(
                    missing,
                    {
                        pid: affiliations[int(pid.removeprefix("paper_"))]
                        for pid, _ in missing
                        if int(pid.removeprefix("paper_")) in affiliations
                    },
                )
                scores.update(follow_up)
                for pid, _ in missing:
                    if pid in follow_up:
                        unassessable_reasons.pop(pid, None)
                    elif not follow_up_responded:
                        unassessable_reasons[pid] = (
                            "follow-up request failed after retries"
                        )

            for pid, paper in batch:
                score = scores.get(pid)
                identifier = _stable_identifier(paper)
                if score is None:
                    zero_scored.append(paper.title)
                    paper.influence_score = 0
                    reason = unassessable_reasons[pid]
                    lines_for_paper = (
                        f"- {identifier} | {paper.title} | unassessable: {reason}"
                    )
                else:
                    paper.influence_score = int(score)
                    lines_for_paper = (
                        f"- {identifier} | {paper.title} | score: {int(score)}"
                    )
                paper_assessments.append(lines_for_paper)

        lines = [
            "Per-paper assessments:",
            *paper_assessments,
            f"Papers: {total}, scored: {total - len(zero_scored)}, "
            f"zero-scored (unassessable or failed): {len(zero_scored)}",
        ]
        if zero_scored:
            lines.append("Zero-scored papers:")
            lines.extend(f"- {t}" for t in zero_scored)
        lines.append(f"(stage took {time.monotonic() - t_start:.1f}s)")
        return data, lines
