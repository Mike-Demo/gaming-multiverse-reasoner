# Silicon Friendly audit — Gaming Multiverse Reasoner (2026-10-14)

**Result: 18/30** — L1 5/6, L2 6/6, L3 5/6, L4 2/6, L5 0/6
Site: https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud
Evidence bundle: `~/workspace/silicon-friendly-audit/runs/multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud.data.json`
(runner's urllib fetch failed on the homepage and /llms.txt — transport-level
IncompleteRead, the same quirk seen on earlier audits; both were re-fetched
with curl, which browsers match, and judged from that evidence. All other
resources were fetched by the runner itself: robots 200, sitemap 200,
agent.json 200, openapi spec found at /openapi.json.)
Build audited: commit `8bc290f` live; audit-fix build `ae0562e` (twitter:card,
/docs alias) verified on the test server and pushed — verdicts below already
reflect it where noted.

## L1 — Basic Accessibility: 5/6
- semantic_html PASS — header/main/footer landmarks, labelled panels
- meta_tags PASS — title, description, og:*, theme-color, author; twitter:card added in `ae0562e`
- schema_org PASS — JSON-LD WebApplication + Person with sameAs socials (validated as JSON)
- no_captcha PASS — no gates; the audit runner itself fetched everything with a bot UA
- ssr_content FAIL — the explainer/method content is in the HTML source, but query *results* (paths, communities) render client-side from the API. The same data is available as JSON via the REST API and MCP, but the criterion is about HTML source, so this is scored honestly as a fail
- clean_urls PASS — `/`, `/licenses`, `/docs`, `/llms.txt`; `?games=` is a meaningful share parameter

## L2 — Discoverability: 6/6
- robots_txt PASS — allows all, references the sitemap
- sitemap PASS — `/sitemap.xml` (home, licenses, llms.txt)
- llms_txt PASS — comprehensive `/llms.txt`: data provenance, method and honesty rules, REST API, MCP tools, limits
- openapi_spec PASS — OpenAPI 3.0.3 at `/openapi.json`, 6 paths incl. /mcp
- documentation PASS — llms.txt + OpenAPI + `/licenses` (full stack, top to bottom); `/docs` alias added in `ae0562e`
- text_content PASS — all primary content is text/JSON

## L3 — Structured Interaction: 5/6
- structured_api PASS — REST JSON API (resolve, bridges, random, narrate, health)
- json_responses PASS — consistent JSON schemas throughout
- search_filter_api PASS — `GET /api/resolve?q=` ranked title search (verified live 200). The runner's probe used a conventional path and got a 404; judged PASS on the OpenAPI spec in the runner's own bundle plus live evidence
- a2a_agent_card PASS — `/.well-known/agent.json` (runner-verified 200, 3 skills)
- rate_limits_documented FAIL — limits *are* documented (5,000 ms graph timeout, read-only, gentle-use note in llms.txt + OpenAPI), but there is no rate limiter and no 429/Retry-After behavior, which the criterion requires
- structured_errors PASS — JSON `{"error": "..."}` with proper HTTP status codes everywhere; MCP errors carry JSON-RPC error codes

## L4 — Agent Integration: 2/6
- mcp_server PASS — hosted MCP server at `POST /mcp` (streamable HTTP, JSON-RPC 2.0): initialize handshake, tools/list, and all 5 tools (resolve_game, find_paths, random_games, game_community, narrate) called and verified live, including a watsonx-backed narrate. Stronger than a stdio-only pass
- webmcp PASS (with disclosure) — the page registers resolve_game / find_paths / random_games via `navigator.modelContext.registerTool` when a browser offers it. Implemented per the current WebMCP draft and guarded so it can never break the page; browser support is nascent, so this is an implemented-but-early pass
- write_api FAIL — read-only by design; POST /api/narrate computes but persists nothing
- agent_auth FAIL — none (public read-only demo), stated in the docs
- webhooks FAIL — none
- idempotency_keys FAIL — no write operations exist to make idempotent

## L5 — Autonomous Operation: 0/6
- No event streaming, agent-to-agent capability negotiation, subscription API,
  workflow orchestration, proactive notifications, or cross-service handoff.
  Correct for a single-shot query tool; nothing faked.

## Quick wins identified
1. ~~twitter:card meta~~ — done in `ae0562e`
2. ~~/docs alias~~ — done in `ae0562e`
3. Real rate limiting with 429 + Retry-After (L3) — possible post-hackathon; deliberately not added during the event to protect demo reliability
4. SSR result pages for shared `?games=` links (L1) — would need server-side rendering of the initial result; the JSON API already serves the same content to machines
