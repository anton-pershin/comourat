"""Paper discovery via an LLM agent with web search."""

from __future__ import annotations

import logging
import time

from pydantic_ai import Agent, AgentRunResult
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from comourat.enrichment.web_tools import attach_web_tools
from comourat.models import PaperCandidateCollection

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a research assistant finding influential academic papers.

You receive the description of a research question, technical challenge, or research
topic, and optionally a list of constraints. Your task is to find the most influential
published papers on it using the available tools:
- `web_search`: search the web (use several queries: general, specific, survey-oriented).
- `extract_url`: fetch and read a promising page (a paper's landing page, arXiv abstract,
  survey, or a reading list) to confirm what the paper is about.

Find the papers that are widely recognised as important or foundational work on the
subject \u2014 seminal papers, highly cited surveys, or landmark results. Prefer papers you
can confirm by reading a page about them over papers you merely recall.

For each paper return:
- `title`: the paper's title as the source page gives it.
- `authors`: the authors, as far as you could establish them.
- `description`: 2-4 sentences describing what the paper does and why it matters,
  drawn from what you read.
- `urls_seen`: the urls you encountered for this paper (landing pages, PDFs, reviews).

Do NOT return DOIs, arXiv ids or other identifiers \u2014 they are not collected.
Only include a paper when you can name it precisely; do not guess titles.
"""


class PaperDiscovererCore:
    """Discovers candidate papers with a web-search-equipped LLM agent.

    The agent is the authority on *which* papers are worth collecting and on
    nothing else: its output model carries title, authors, description and
    urls only, so any identifier the model emits is rejected by validation
    rather than dropped by a discard step.
    """

    def __init__(
        self,
        model: Model,
        attribute_description: str,
        constraints_description: str = "",
        candidate_model: type = None,
        system_prompt: str = SYSTEM_PROMPT,
        model_settings: dict | None = None,
        retries: int | None = None,
        request_limit: int = 30,
        tool_calls_limit: int = 15,
        tool_call_rejection_buffer: int = 4,
    ) -> None:
        self.attribute_description = attribute_description
        self.constraints_description = constraints_description

        if candidate_model is None:
            candidate_model = PaperCandidateCollection
        self.agent: Agent = Agent(
            model,
            output_type=candidate_model,
            system_prompt=system_prompt,
            model_settings=model_settings,
            retries=retries,
            deps_type=dict,
        )
        attach_web_tools(
            self.agent, tool_call_rejection_buffer=tool_call_rejection_buffer
        )

        self.request_limit = request_limit
        self.tool_calls_limit = tool_calls_limit

    def build_prompt(self) -> str:
        lines = [
            "Find the most influential published papers on the following:",
            "",
            self.attribute_description,
        ]
        if self.constraints_description:
            lines += [
                "",
                "Constraints the papers should satisfy:",
                self.constraints_description,
            ]
        lines += [
            "",
            "Return the list of papers (title, authors, description, urls seen).",
        ]
        return "\n".join(lines)

    def discover(self) -> tuple[PaperCandidateCollection, list[str]]:
        """Run the discovery agent; returns (candidates, summary_lines)."""
        prompt = self.build_prompt()

        tools_before = self.agent._comourat_tool_time_total  # type: ignore[attr-defined]
        t_run = time.monotonic()
        try:
            run_result: AgentRunResult = self.agent.run_sync(
                prompt,
                usage_limits=UsageLimits(
                    request_limit=self.request_limit,
                    tool_calls_limit=self.tool_calls_limit,
                ),
            )
        except UsageLimitExceeded as e:
            logger.exception(
                "Tool or request usage limit exceeded during discovery: %s", e
            )
            raise
        run_s = time.monotonic() - t_run
        tools_s = (
            self.agent._comourat_tool_time_total - tools_before  # type: ignore[attr-defined]
        )
        logger.debug(
            "discover | total=%.2fs tools=%.2fs llm=%.2fs",
            run_s,
            tools_s,
            run_s - tools_s,
        )

        result: PaperCandidateCollection = run_result.output
        # Discovery is not an identifier authority. Canonical identifiers are
        # accepted only from graph-client records or later deterministic lookup.
        for paper in result.papers:
            paper.arxiv_id = None
            paper.doi = None

        lines = [f"Discovered {len(result.papers)} papers in {run_s:.1f}s"]
        for p in result.papers:
            urls = ", ".join(p.urls_seen) or "(no urls)"
            lines.append(
                f"### {p.title}\nAuthors: {', '.join(p.authors) or '(unknown)'}\n"
                f"URLs seen: {urls}\n{p.description}\n"
            )
        return result, lines
