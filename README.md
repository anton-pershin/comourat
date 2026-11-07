# comourat

Comourat is a Python library for collecting and processing research-related content. It provides reusable components for external content sources, paper and post collection, paper resolution, influence assessment, arXiv PDF URL verification, and web-content enrichment.

Comourat is a library, not a pipeline runner. Callers create and configure the components they need through Python interfaces. Components do not require a configuration framework, database, or output writer.

## Features

- Access paper metadata and citation graphs through arXiv, Semantic Scholar, and OpenAlex clients.
- Search Semantic Scholar and convert its results to paper information models.
- Discover and expand paper candidates, including forward citation expansion, backward reference expansion, and title-based relevance search.
- Resolve paper candidates to source records and check for matching arXiv PDF URLs.
- Calculate post and citation influence scores and assess author authority.
- Collect subreddit feeds or one specified Reddit post without collecting comments.
- Search the web, extract page content, and add web-derived context to content items.
- Distinguish enrichment context, successful no-context, and sanitized failure results.
- Summarize content through explicit success, empty-summary, provider-failure, and timeout results.
- Use neutral Pydantic models for papers, posts, and enrichment results.
- Enforce destination, redirect, DNS, trusted-proxy, and overall-deadline rules on article and enrichment fetches.

## Requirements

- Python 3.12 or later.
- Requests 2.32.3 or later and urllib3 2.x for protected fetching.
- pydantic-ai 2.64.0 or later for public enrichment and summarization with overall deadlines.

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

## Example: get arXiv title and abstract

```python
from comourat.clients.arxiv import ArxivClient

client = ArxivClient(user_agent="Your Project (contact@example.org)")
paper = client.get_metadata("https://arxiv.org/pdf/1706.03762")

if paper is not None:
    print(paper.title)
    print(paper.abstract)
```

`get_metadata` returns a `PaperRecord` with its arXiv identity, title, and abstract populated. Other record fields keep their defaults. It accepts current and legacy arXiv identifiers, with an optional `arXiv:` prefix and version suffix. It also accepts HTTP and HTTPS abstract and PDF URLs on `arxiv.org` and `www.arxiv.org`. PDF URLs can include `.pdf`; URL query strings and fragments are ignored.

The client extracts the identifier locally and requests only the arXiv Atom API. It does not request or extract the paper body. An unknown paper with an empty Atom feed returns `None` from both `get_metadata` and `get_title_by_id`. An invalid metadata reference raises `ValueError`. Malformed XML or missing title or abstract fields in `get_metadata` raise `ArxivMetadataError`.

Both `get_metadata` and `get_title_by_id` reject an arXiv provider error entry identified by an `/api/errors` entry ID or link. They raise `ArxivMetadataError` with the provider summary as diagnostic context instead of returning the error as paper data. Detection does not use the entry title, so an ordinary paper titled `Error` remains valid. After the configured retries, provider HTTP and transport failures remain explicit as `requests.HTTPError`, `requests.ConnectionError`, or `requests.Timeout`.

Both `timeout_seconds` and `overall_timeout_seconds` default to 120 seconds. `timeout_seconds` is the connection and read timeout for each HTTP request. `overall_timeout_seconds` limits the complete operation, including HTTP requests, Atom response parsing, retries, backoff, and politeness delays. Each request and delay receives only the remaining time. Overall expiry raises `requests.Timeout`. These limits also apply to `get_title_by_id` and `search_by_title`.

## Package layout

- `comourat.clients` — external content-source clients.
- `comourat.collectors` — paper and post collection.
- `comourat.models` — shared Pydantic data models.
- `comourat.resolution` — paper-candidate resolution.
- `comourat.verification` — arXiv PDF URL verification.
- `comourat.influence` — post, citation, and authority influence assessment.
- `comourat.enrichment` — web search, page extraction, and content enrichment.

## Collect one specified Reddit post

Use `RedditSubmissionCollector` with a `RedditSubmissionClient` to collect one caller-selected post without collecting comments:

```python
from comourat.clients.reddit import RedditSubmissionClient
from comourat.collectors.reddit import RedditSubmissionCollector

client = RedditSubmissionClient(
    user_agent="Your Project (contact@example.org)",
    connection_timeout=10,
    operation_timeout=30,
)
collector = RedditSubmissionCollector(client)
post = collector.collect("https://www.reddit.com/r/python/comments/abc123/a_post/")
print(post.title, post.url, post.text)
```

`collect` accepts a bare ASCII base36 submission ID, a Reddit comment permalink, a Reddit gallery URL, or a `redd.it` short URL. HTTP and HTTPS input URLs are accepted because Comourat extracts only the ID; it does not fetch the input URL. The URL must contain the submission ID directly. Mobile share URLs and other URLs that require resolution are not supported.

The client requests only Reddit's `/api/info/` submission listing. It does not use PRAW's lazy submission loader, request a subreddit feed, or request comments. A link post with empty `selftext` is valid.

The returned `RedditPostInfo` contains the subreddit, bare submission ID, title, author, UTC creation time, text, and score. Its `url` is the absolute `https://www.reddit.com` discussion permalink from the source response, even when the submission links to an external article.

The default API base URL is `https://www.reddit.com`. Pass `api_url` to use another caller-selected Reddit API endpoint. Pass a configured Requests session as `http_session` when source authentication headers or other explicit session settings are needed. Use a separate session for each client. Environment proxy and timeout settings are not used.

Failures have these public outcomes:

- `ValueError`: invalid reference, user agent, API URL, or timeout configuration.
- `LookupError`: unavailable, deleted, removed, empty, or mismatched submission.
- `RuntimeError`: other provider, HTTP, JSON, response-format, or source-data failures.
- `TimeoutError`: connection or overall operation timeout expiry.

Translated exceptions preserve the original exception as `__cause__`. Failures never produce an empty or partial successful result.

The connection timeout defaults to 10 seconds and applies to each connection attempt. The overall operation timeout defaults to 30 seconds and includes request preparation and authentication hooks, DNS resolution, connections, TLS handshakes, request and response transfer, redirects, and response-body reading. Each stage receives only the remaining overall time. A redirect, new connection, or response read does not restart it. Both values must be positive finite numbers.

## Public enrichment

Use `EnrichmentAgent.enrich` to gather web-derived context with a caller-configured `pydantic-ai` model. Pass the model, prompt, and optional settings directly. Comourat does not read provider credentials or timeout settings from environment variables or configuration files.

```python
from comourat.enrichment.enricher import EnrichmentAgent
from comourat.models import (
    EmptyEnrichment,
    EnrichmentFailure,
    EnrichmentSuccess,
)

enricher = EnrichmentAgent(
    model,  # A pydantic-ai Model configured by the caller.
    overall_timeout=120,
)
result = enricher.enrich(prompt)

if isinstance(result, EnrichmentSuccess):
    context_points = result.additional_context
elif isinstance(result, EmptyEnrichment):
    context_points = []
else:
    assert isinstance(result, EnrichmentFailure)
    error_category = result.error_category
    error_message = result.error_message
```

The public result types are Pydantic models. `EnrichmentResult` is their discriminated union. The `outcome` field identifies each result:

| Result | Meaning |
|---|---|
| `EnrichmentSuccess(outcome="context")` | Enrichment succeeded and `additional_context` contains one or more context points. |
| `EmptyEnrichment(outcome="no_context")` | Enrichment succeeded and `additional_context` is empty. This is not a failure. |
| `EnrichmentFailure(outcome="failure")` | Search, fetch, provider, usage-limit, or timeout failure. No successful empty result is returned. |

A successful search with no results and a successful extraction with no extractable content are normal tool outcomes. They do not cause a failure by themselves. Search and extraction exceptions are not returned as ordinary text to the model. They produce failure results instead.

Failure categories and messages are fixed:

| `error_category` | `error_message` |
|---|---|
| `search` | `Enrichment web search failed.` |
| `fetch` | `Enrichment URL extraction failed.` |
| `provider` | `Enrichment provider failed.` |
| `usage_limit` | `Enrichment usage limit exceeded.` |
| `timeout` | `Enrichment exceeded the overall deadline.` |

Public failures do not contain exception objects, provider error text, credentials, request data, prompts, sensitive URL components, or raw responses.

`overall_timeout` defaults to 120 seconds. One monotonic deadline covers each complete agent run, including model requests, web tools, and output validation. The agent receives only the remaining time. Output produced after the deadline returns the `timeout` failure instead of a context or no-context result. The configured `FetchPolicy` continues to bound each HTTP request within the agent run.

`overall_timeout` must be a positive finite number. Zero, negative, non-finite, non-numeric, and Boolean values raise `ValueError` before Comourat creates an agent.

## Public summarization

Use `comourat.enrichment.summarizer.summarize` to summarize content with a caller-configured `pydantic-ai` model. Pass the model and content directly. Optional arguments configure the system prompt, model settings, non-empty policy, retries, retry delay, and overall timeout. Comourat does not read these settings or provider credentials from environment variables or configuration files.

```python
from comourat.enrichment.summarizer import (
    EmptySummary,
    SummaryFailure,
    SummarySuccess,
    summarize,
)

result = summarize(
    model,  # A pydantic-ai Model configured by the caller.
    content,
    require_non_empty=True,
    retries=2,
    retry_delay=0.5,
    overall_timeout=30,
)

if isinstance(result, SummarySuccess):
    summary_text = result.summary
elif isinstance(result, EmptySummary):
    summary_text = None
else:
    assert isinstance(result, SummaryFailure)
    error_category = result.error_category
    error_message = result.error_message
```

The public result types are Pydantic models. The `outcome` field identifies each result:

| Result | Meaning |
|---|---|
| `SummarySuccess(outcome="success")` | The provider succeeded. `summary` contains trimmed non-empty text. If `require_non_empty=False`, an empty provider response instead gives `summary=""`. |
| `EmptySummary(outcome="empty")` | The provider returned empty or whitespace-only text, but the default `require_non_empty=True` policy rejected it. `summary` is empty. This outcome is not a provider failure and does not cause a retry. |
| `SummaryFailure(outcome="failure", error_category="provider")` | The permitted provider attempts failed. `error_message` is `"Summarization provider failed."` |
| `SummaryFailure(outcome="failure", error_category="timeout")` | The overall deadline ended the operation. `error_message` is `"Summarization exceeded the overall deadline."` |

Public failures contain only these fixed categories and messages. They do not contain provider exception objects, provider error text, credentials, request headers, prompts, or raw provider responses.

`retries` counts attempts after the first attempt. The default `retries=1` permits at most two provider calls. `retry_delay` is the requested delay between failed attempts and defaults to zero. `overall_timeout` defaults to 30 seconds. One monotonic deadline covers every provider wait and retry delay. Each attempt and delay receives only the remaining time. A retry does not reset or extend the deadline, and no new attempt starts after it expires. The retry count and deadline are independent upper bounds.

`retries` must be a non-negative integer. `retry_delay` must be non-negative and finite. `overall_timeout` must be positive and finite. Invalid values raise `ValueError` before Comourat creates an agent or calls a provider.

## Research-article extraction

`PageFetcher.fetch` returns an `ArticleResult` for a research-related public blog or article URL. The result contains a non-empty title, the source URL, and the extracted main text. Comourat does not summarize the text. It removes leading and trailing whitespace from the title and text. After a redirect, `source_url` is the final response URL. If the response does not supply a URL, it is the validated input URL.

```python
from comourat.enrichment.page_fetch import ArticleFetchError, PageFetcher

fetcher = PageFetcher(connection_timeout=10, operation_timeout=30)
try:
    article = fetcher.fetch("https://example.org/research/article")
    print(article.title)
    print(article.source_url)
    print(article.text)
except ArticleFetchError as exc:
    print(f"Article extraction failed: {exc.category}")
```

The default title extractor uses Trafilatura metadata. It falls back to the HTML document title and then the first `h1` heading. The `extractor` and `title_extractor` constructor arguments support controlled extraction when needed.

Expected failures raise `ArticleFetchError`. Its `category` is one of:

| Category | Meaning |
|---|---|
| `invalid_input` | The URL is malformed, or the local fetch policy denies the target. |
| `unsupported_scheme` | The direct or redirected target does not use HTTP or HTTPS. |
| `unavailable` | The source returned HTTP 404 or 410. |
| `empty_extraction` | The extracted title or text is missing, empty, or whitespace-only. |
| `timeout` | A connection, response, overall-operation, or extraction-wait deadline expired. |
| `provider` | Another HTTP, DNS, connection, response, or source-request failure occurred. |
| `extraction` | A title or text extractor failed or returned a value of the wrong type. |

Error messages are fixed Comourat messages. They do not copy provider error text, response content, URL userinfo, paths, queries, or credentials. No failure returns a partial `ArticleResult`.

The default connection timeout is 10 seconds for each connection attempt. The default overall operation timeout is 30 seconds. The overall limit starts before source retrieval and includes DNS, source and proxy connections, request and response transfer, redirects, retries, and the wait for local title and text extraction. Each stage uses only the remaining time. A late extraction result is discarded and produces the `timeout` category.

`timeout` remains an alias for `operation_timeout`. Do not pass `timeout`, `connection_timeout`, or `operation_timeout` together with `fetch_policy`; the supplied policy controls both limits instead. To allow connection attempts to use all remaining operation time without a separate connection cap, pass `FetchPolicy(connection_timeout=None, overall_timeout=...)`. Constructor configuration errors continue to raise their configuration exceptions.

## Protected web fetching

`PageFetcher` and the web tools used by `create_enrichment_agent`, `EnrichmentAgent`, and `PaperDiscovererCore` use `SafeHttpClient`. The default `FetchPolicy` permits only HTTP and HTTPS targets with global unicast addresses. It rejects loopback, private, link-local, unspecified, multicast, reserved, carrier-grade NAT, and other non-global IPv4 and IPv6 addresses. IPv4-mapped IPv6 addresses are checked in both address families.

Pass a policy to add exact-host, IP-address, or CIDR deny rules. Host rules ignore case, IDNA differences, and a trailing dot. For example:

```python
from comourat.enrichment.page_fetch import PageFetcher
from comourat.fetch import FetchPolicy

policy = FetchPolicy(
    deny_hosts=["tts.internal"],
    deny_addresses=["10.0.0.0/8", "fd00::/8"],
    overall_timeout=30,
    max_redirects=30,
    max_retries=0,
)
fetcher = PageFetcher(
    fetch_policy=policy,
    proxy="socks5h://proxy.internal:1080",
)
```

Do not put real proxy credentials in source control. The same `http_session`, `fetch_policy`, `proxy`, and `resolver` arguments are available on `PageFetcher`, `attach_web_tools`, `create_enrichment_agent`, `EnrichmentAgent`, and `PaperDiscovererCore`. A protected client disables environment-proxy discovery on its session and replaces the HTTP and HTTPS adapters. Use a separate session for each protected client. Per-request proxies and proxy-bypass rules are not supported.

### DNS and trusted proxies

The proxy scheme determines which component resolves the target hostname:

| Request configuration | Target DNS and resolved-address checks |
|---|---|
| No proxy | Comourat resolves and validates every answer. The socket connects to a validated address. |
| `socks5` or `socks4` proxy | Comourat resolves and validates every answer, then sends a validated IP address to the proxy. |
| `http` or `https` proxy | The proxy resolves the hostname. Comourat does not perform local target DNS. |
| `socks5h` proxy | The proxy resolves the hostname. Comourat does not perform local target DNS. |

An explicitly configured proxy is trusted infrastructure and may have a loopback or private address. Comourat validates and pins the proxy endpoint and applies caller deny rules to it. For HTTP forwarding, HTTP CONNECT, and `socks5h`, the caller must configure the proxy or its network firewall to enforce the same resolved-address and CIDR rules. This includes the default prohibited addresses and every caller-provided IP or CIDR denial. Comourat cannot inspect the remote DNS answer. Scheme, exact-host, and literal-IP checks still run in Comourat before a proxy connection. Every redirect follows the same division of responsibility. Environment-provided proxies are not part of this trust model and are ignored.

SOCKS4 supports only IPv4 targets. An IPv6 target through SOCKS4 is rejected before connection. `socks5h` retains its standard remote-DNS meaning; `socks5` retains local resolution. The optional resolver is a callable `(host, port) -> iterable` that returns `socket.getaddrinfo`-style answers. Inject it when direct or local-SOCKS DNS must use a trusted resolver instead of the system resolver.

### Overall deadline and failures

Each top-level request has one monotonic overall deadline. The default is 30 seconds. DNS resolution, direct and proxy connections, proxy negotiation, TLS handshakes, request and response transfer, response-body reading, redirects, retries, and retry delays all consume the same budget. Each stage receives only the remaining time. A slow peer cannot restart the timeout by sending data slowly.

The default redirect limit is 30 and automatic retries are disabled (`max_retries=0`). `FetchPolicy(connection_timeout=...)` optionally caps each connection attempt without exceeding the remaining overall time. Its default `None` preserves the overall-timeout-only behavior. The direct `PageFetcher` timeout settings and aliases are described in the article-extraction section.

`SafeHttpClient` raises `FetchPolicyError` for local policy denials, `FetchResolutionError` for resolution failures, and `FetchDeadlineExceeded` when the overall deadline expires. Proxy-enforced denials can appear as proxy or HTTP errors. No failure selects an unprotected direct or proxy fallback. Logs and client exceptions remove URL userinfo, paths, queries, and proxy credentials. `PageFetcher` translates these failures into its public article categories. `EnrichmentAgent` converts protected search and fetch exceptions into the public failure results described above. Direct `attach_web_tools` use keeps its string-returning default for other agents, including paper discovery.

## Tests

Run the test suite from the repository root:

```bash
python -m pytest -q
```

The tests use mocked source responses and do not require live network access.

## Configuration and data boundaries

Pass configuration to the component that uses it. Comourat does not load settings from environment variables or configuration files. Its models represent source and processing data; application-specific research context and relevance scores belong in the consuming application.
