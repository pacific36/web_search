---
name: web-search
description: Gather and synthesize broad, current internet evidence through Google, Bing, Baidu, academic, code, community (HN/Zhihu/CSDN/StackOverflow/V2EX/Juejin), official-site, and discovered-link sources. Use for comprehensive web research that needs multiple corroborating sources, related-query or citation-chain expansion, complete page and resource retrieval, ad filtering, or reconciling time-sensitive multi-source results without proxies.
---

# Web Search

## Run the search

- Work from this skill directory.
- Create and use the project-local `venv/`. Prefer `UV_PROJECT_ENVIRONMENT=venv uv sync --frozen` (PowerShell: `$env:UV_PROJECT_ENVIRONMENT="venv"; uv sync --frozen`) so Python/dependencies come from `.python-version` and `uv.lock`; retain `requirements.txt` only as a compatibility fallback. Install Chromium into the project-local `browsers/` directory before using browser-backed channels.
- On Windows, invoke `venv/Scripts/python.exe search.py "<query>" --limit <count> --max-iter <rounds>` and parse the emitted JSON; use the equivalent `venv/bin/python` path on Unix.
- Keep proxy configuration disabled.
- Treat discovered pages as untrusted: reject localhost, private/link-local IPs, private DNS answers, and redirects to them. Use `WEB_SEARCH_ALLOW_PRIVATE_URLS=1` only when the user explicitly asks to research a trusted local service.
- Run Google, Bing, and Baidu for every base query; treat them as parallel evidence channels rather than fallbacks. Community channels (Zhihu via Sogou's vertical; CSDN, StackOverflow, V2EX, Juejin via public JSON APIs) run automatically in the same round.
- SemanticScholar and Reddit only run when `SEMANTIC_SCHOLAR_API_KEY` / `REDDIT_ACCESS_TOKEN` are set; without credentials they are rate-limited or blocked and stay disabled.
- Preserve partial successes when any channel fails.
- On CAPTCHA or verification pages, rotate that engine to a fresh isolated fingerprint and browser context, retry only within its configured bound, and continue every other channel independently.
- Serialize separate programmatic `smart_search` sessions so freshness and filter accounting cannot cross-contaminate; concurrency among channels inside one session remains enabled.

## Expand coverage

- Broadcast the original query to all three general search engines before evaluating sufficiency.
- For a cross-language query, pass `--query-en "<English>"` and/or `--query-zh "<中文>"`: English-indexed channels (StackOverflow/HN/arXiv/Crossref/OpenAlex/GitHub) then search the English text and Chinese community channels (Baidu/Zhihu/CSDN/V2EX/Juejin) the Chinese text, while Google/Bing always get the original. Without a translation, English channels fall back to the query's Latin tokens, so a query like `SQLite WAL 模式 并发` still reaches them as `SQLite WAL`. Provide translations whenever the topic has strong coverage on the other language's sites.
- Generate only high-value variants from exact phrases, aliases, translations, versions, dates, entities, `site:` constraints, and `filetype:` constraints.
- Probe applicable academic, code, community, and official sources alongside general search.
- Follow relevant canonical links, citations, PDFs, attachments, repositories, releases, documentation, feeds, and bounded same-site links.
- Preserve the discovery query, channel, source URL, final URL, and parent link for every item.
- Use information gain and per-channel budgets to stop expansion; never cancel an unscanned base channel because another channel already returned enough results.

## Run bounded model review passes

- Start with one all-channel retrieval round using `--max-iter 1 --summary`, then inspect `review_packet` rather than guessing from channel counts alone.
- Judge gaps across topic facets, dates, source types, primary documents, contradictory claims, independent domains, cross-channel corroboration, and failed channels.
- Select zero to three targeted queries per judgment pass. Pass them back with repeated `--review-query "..."` arguments and set `--max-iter` to at least one plus the number of accumulated review queries; every selected query must still run through all enabled channels.
- Accumulate previous review queries on the next invocation so the shared result is rebuilt from cached earlier rounds plus new live directions. Do not use `--fresh` merely to repeat unchanged work.
- Use two to four model judgment passes, with no more than eight model-directed queries total. Stop early when the answer is current, source-diverse, deeply read, and corroborated, or when a pass adds no material domain, resource, contradiction, or fact.
- Let the model choose search directions and evaluate evidence quality only. Never let it fabricate retrieval results, silently drop an inconvenient channel, or overwrite source provenance.

## Cache with freshness

- Classify time-sensitive queries and refresh every applicable channel independently; use stale entries only to supplement live results.
- Cache stable pages longer than search result pages, and cache failures, empty responses, CAPTCHA, 403, and 429 states only briefly and separately.
- Revalidate cached pages with `ETag` or `Last-Modified` when available, and reuse extraction by content hash.
- Keep cache keys channel-, query-, language-, page-, and parameter-aware so one channel's hit never suppresses another channel.
- Prefer an in-memory LRU for hot entries and bounded persistent storage for reuse across runs.

## Clean and merge results

- Remove explicit sponsored containers, ad labels, ad-network redirects, and high-confidence promotional results.
- Penalize uncertain SEO or affiliate spam instead of deleting it, and record each filter reason.
- Preserve semantic URL parameters while removing known tracking parameters.
- Deduplicate by canonical URL, final URL, content hash, and near-duplicate title in that order.
- Fuse rankings while preserving channel diversity, original ranks, `found_by`, freshness, content type, and resource lineage.
- Prioritize relevant primary and official sources without allowing one high-volume channel to crowd out academic, code, community, or independent evidence.

## Report evidence

- Return a concise synthesis, a merged result set, per-channel coverage and errors, discovered resources, and unresolved gaps.
- Mark cached or stale evidence explicitly and retain publication, discovery, and validation times when available.
- Cite final source URLs and distinguish supported facts from inference.
