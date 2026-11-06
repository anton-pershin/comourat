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
- Use neutral Pydantic models for papers, posts, and enrichment results.
- Enforce destination, redirect, DNS, trusted-proxy, and overall-deadline rules on article and enrichment fetches.

## Requirements

- Python 3.12 or later.
- Requests 2.32.3 or later and urllib3 2.x for protected fetching.

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

The default redirect limit is 30 and automatic retries are disabled (`max_retries=0`). `FetchPolicy(connection_timeout=...)` optionally caps each connection attempt without exceeding the remaining overall time. Its default `None` preserves the overall-timeout-only behavior. `PageFetcher(timeout=...)` remains available as an alias for configuring the default policy timeout. Do not pass `timeout` and `fetch_policy` together.

`SafeHttpClient` raises `FetchPolicyError` for local policy denials, `FetchResolutionError` for resolution failures, and `FetchDeadlineExceeded` when the overall deadline expires. Proxy-enforced denials can appear as proxy or HTTP errors. No failure selects an unprotected direct or proxy fallback. Logs and client exceptions remove URL userinfo, paths, queries, and proxy credentials. Existing `PageFetcher` and enrichment-tool result contracts remain unchanged.

## Tests

Run the test suite from the repository root:

```bash
python -m pytest -q
```

The tests use mocked source responses and do not require live network access.

## Configuration and data boundaries

Pass configuration to the component that uses it. Comourat does not load settings from environment variables or configuration files. Its models represent source and processing data; application-specific research context and relevance scores belong in the consuming application.
