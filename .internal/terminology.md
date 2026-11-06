## Terminology

| Term | Definition |
|------|------------|
| Content item | Research-related content that a library component collects or processes. |
| Content source | An external service or resource that provides content. |
| Content-source client | A component that communicates with a content source. |
| Content collector | A component that collects content from one or more sources. |
| Content processing | An operation that resolves, assesses, verifies, or enriches collected content. |
| Data model | A structured representation of data exchanged by library components. |
| Paper identity | The identifiers used to identify a paper, such as an arXiv ID or DOI. |
| Paper record | A normalized record for a paper, including its identity and available metadata. |
| Paper page | One page of paper records returned by a source, with continuation data when more results are available. |
| Paper candidate | A paper proposed by a collection or discovery component before resolution. |
| Paper resolution | Matching a paper candidate to a paper record from a source. |
| Resolved paper | A paper record matched to a paper candidate. |
| Title similarity threshold | The minimum title similarity required to accept a candidate-to-record match. |
| Paper discovery | Search for paper candidates related to a caller's research needs. |
| Seed item | A paper used as a starting point for discovering related papers. |
| Seed expansion | Discovery of paper candidates related to one or more seed papers. |
| Forward citation expansion | Discovery of papers that cite a seed paper. |
| Backward reference expansion | Discovery of papers cited by a seed paper. |
| Relevance search | A source search using a seed paper's title to find related papers. |
| Candidate provenance | The discovery methods that produced a paper candidate. |
| Content enrichment | Additional context or a summary added to collected content. |
| Linked content | The text extracted from a web page linked to a content item. |
| Influence score | A measure of a content item's impact or authority, represented on a 0–100 scale where applicable. |
| Post influence | An influence score calculated from a post's raw score and a reference level. |
| Raw post score | A source-provided engagement value, such as points or votes, before normalization. |
| Reference level | A configured raw post score used to normalize post influence. |
| Citation influence | An influence score estimated from a paper's citation impact. |
| Field-weighted citation impact (FWCI) | A provider's citation-impact measure normalized for field and publication age. |
| Citations per year | A citation rate derived from citation count and publication age. |
| Breakpoint table | Configured input values and corresponding influence scores used for interpolation. |
| Authority influence | An assessment of author and affiliation authority recorded as a paper's influence score. |
| Authority verdict | A model-produced score and explanation for a paper's author and affiliation authority. |
| Fail-open scoring | Assigning a score of zero when authority assessment cannot produce a usable result. |
| Verified arXiv PDF | An arXiv PDF URL confirmed to match a paper record. |
| New submission | An arXiv RSS entry announced as new, rather than as a replacement or cross-listing. |
| Author affiliation | An institution associated with an author, extracted from a paper page. |
| Reddit post | A post collected from Reddit, with its source identifiers and available engagement data. |
| Hacker News story | A Hacker News submission with its title, URL, points, and comment count. |
| Pagination continuation | A source-provided value used to request the next page of results. |
| Proxy URL | A URL that specifies the proxy used for outgoing HTTP requests. |
| Fetch policy | Caller-provided rules that control target URLs, denied destinations, redirects, retries, and the overall deadline for a fetch. |
| Connection binding | Use of the IP address validated by the DNS-resolving component for the actual connection, without an independent second resolution. |
| Overall deadline | One monotonic time limit shared by every stage of a top-level fetch, including DNS, connection setup, response transfer, redirects, and retries. |
| Trusted-proxy DNS | Target hostname resolution by an explicitly configured caller-hosted proxy that applies equivalent resolved-address and CIDR deny rules through the proxy or its network. |
| Article result | The title, final source URL, and non-empty extracted text of a research-related public article. |
| Article failure category | A stable public value that identifies an article input, source, timeout, or extraction failure. |
| Connection timeout | The maximum time for one source or proxy connection attempt, limited by the remaining overall deadline. |
| Article operation deadline | One monotonic time limit for source retrieval and the wait for local title and text extraction. |
