# comourat

Comourat is a Python library for collecting and processing research-related content. It provides reusable components for external content sources, paper and post collection, paper resolution, influence assessment, arXiv PDF URL verification, and web-content enrichment.

Comourat is a library, not a pipeline runner. Callers create and configure the components they need through Python interfaces. Components do not require a configuration framework, database, or output writer.

## Features

- Access paper metadata and citation graphs through arXiv, Semantic Scholar, and OpenAlex clients.
- Search Semantic Scholar and convert its results to paper information models.
- Discover and expand paper candidates, including forward citation expansion, backward reference expansion, and title-based relevance search.
- Resolve paper candidates to source records and check for matching arXiv PDF URLs.
- Calculate post and citation influence scores and assess author authority.
- Search the web, extract page content, and add web-derived context to content items.
- Use neutral Pydantic models for papers, posts, and enrichment results.

## Requirements

- Python 3.12 or later.

## Install from a checkout

```bash
python -m pip install .
```

To install an editable checkout for development:

```bash
python -m pip install -e .
python -m pip install pytest
```

## Example: search OpenAlex

```python
from comourat.clients.openalex import OpenAlexClient

client = OpenAlexClient(user_agent="Your Project (contact@example.org)")
page = client.search_papers("retrieval-augmented generation")

for paper in page.papers:
    print(paper.title, paper.identity.doi or paper.identity.arxiv_id)
```

The OpenAlex client uses the public API when no API key is provided. Pass `api_key` to use an API key. Other clients and collectors accept their settings through constructor arguments. Do not put credentials in source control.

## Package layout

- `comourat.clients` — external content-source clients.
- `comourat.collectors` — paper and post collection.
- `comourat.models` — shared Pydantic data models.
- `comourat.resolution` — paper-candidate resolution.
- `comourat.verification` — arXiv PDF URL verification.
- `comourat.influence` — post, citation, and authority influence assessment.
- `comourat.enrichment` — web search, page extraction, and content enrichment.

## Tests

Run the test suite from the repository root:

```bash
python -m pytest -q
```

The tests use mocked source responses and do not require live network access.

## Configuration and data boundaries

Pass configuration to the component that uses it. Comourat does not load settings from environment variables or configuration files. Its models represent source and processing data; application-specific research context and relevance scores belong in the consuming application.
