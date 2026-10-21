# Security audit triage — 2026-10-21

Parent: https://github.com/Mike-Demo/gaming-multiverse-reasoner/issues/1

This is the requested fallback backlog: issue creation is unavailable in this
session. No child issues have been filed. Copy each title and body below into
one issue in this repository. No application fixes are included.

Scope: the parent audit reports **two confirmed findings and three
needs-validation leads**. Evidence was checked by static source inspection
only; no application code was executed and no deployed endpoint was probed.
Source links pin the reviewed revision. Some passages in the parent issue
literally end in an ellipsis; missing-fact statements below preserve that text
verbatim rather than inventing the omitted words.

## Priority and grouping

| Order | Ready-to-file title | Rationale |
| --- | --- | --- |
| 1 | `[security][medium] Ground REST narration in server-computed graph facts` | An unauthenticated caller controls both model facts and their citation trust anchor. With the operator's IBM key configured, this also spends operator quota on caller-authored content. The existing MCP flow supplies a small, established server-side pattern, but the browser request must change too. |
| 2 | `[security][low] Minimize public errors and health diagnostics` | Unauthenticated callers can obtain exception details and backend identifiers. This is information disclosure, not demonstrated credential leakage or code execution. The fix is small and localized, but its lower impact keeps it second. |

These are distinct root causes and should remain two issues. Group the GET,
POST, and MCP exception disclosures with health diagnostics under the second
issue: all expose internal diagnostics across the public response boundary.
The validation leads are not confirmed findings and receive no severity.

## Issue 1 — title

`[security][medium] Ground REST narration in server-computed graph facts`

### Body

Parent audit: https://github.com/Mike-Demo/gaming-multiverse-reasoner/issues/1

**Fingerprint:** `narrate-client-facts-llm-grounding-bypass`

**Status:** Confirmed by static inspection. **Priority:** 1. **Severity:** medium.

#### Evidence and reachability

- The unauthenticated POST handler forwards decoded caller JSON directly to
  `api_narrate`:
  [app.py lines 804–809](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L804-L809).
- `pairs[].paths[]` supplies `path_id`, `kind`, and `description`; pair names
  supply `a_name`/`b_name`. Both model facts and the `known` citation set come
  from this data. If `IBM_API_KEY` is configured and `known` is nonempty,
  `watsonx_claims(facts)` runs. Filtering returned citations against the same
  caller-defined set and limiting text to 400 characters cannot establish graph
  provenance:
  [app.py lines 665–693](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L665-L693).
- MCP already recomputes facts with `api_narrate(api_bridges(ids))`:
  [app.py lines 1678–1682](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L1678-L1682).
- The browser currently posts all of `DATA`:
  [app.py lines 1111–1116](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L1111-L1116).

**Impact:** A lower-trust caller can submit arbitrary purported facts with
self-chosen citations, defeating the grounding control and consuming operator
LLM quota when configured. Without a successful model response, the template
fallback still labels caller-authored facts as graph-derived
([app.py lines 700–706](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L700-L706)).
This does not establish model instruction takeover, victim-browser execution,
or credential theft.

#### Root cause

Caller-provided facts and identifiers serve as both prompt content and the
citation-validation trust anchor, rather than authoritative server graph data.

#### Smallest fix

Make the REST endpoint accept only validated game IDs and compute
`api_bridges(ids)` server-side before narration, matching MCP. Drop
caller-supplied pairs, descriptions, names, and path IDs as narration inputs.
Update the browser request and any API contract documentation describing the
old payload. Authentication alone would not fix the provenance failure.

#### Acceptance criteria for the remediation

- [ ] REST narration uses only server-computed facts and citation IDs.
- [ ] Forged pairs, names, descriptions, and path IDs cannot affect the prompt,
  accepted citations, or template fallback; reject or ignore those fields.
- [ ] Validate the supported 2–4 game IDs before graph/model work.
- [ ] Browser narration and MCP narration still work with server-derived facts,
  including when watsonx is unavailable; use mocked backends for regression tests.

## Issue 2 — title

`[security][low] Minimize public errors and health diagnostics`

### Body

Parent audit: https://github.com/Mike-Demo/gaming-multiverse-reasoner/issues/1

**Fingerprint:** `verbose-error-disclosure`

**Status:** Confirmed by static inspection. **Priority:** 2. **Severity:** low.

#### Evidence and reachability

- GET and POST catch-alls return
  `{"error": f"{type(e).__name__}: {e}"}` with HTTP 500:
  [app.py lines 801–834](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L801-L834).
- The MCP `tools/call` wrapper embeds the same raw type/message in a tool
  result with `isError: true`:
  [app.py lines 1711–1722](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L1711-L1722).
- Public `/api/health` returns graph name, game count, and configured reader
  identity:
  [app.py lines 766–770](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L766-L770).

**Impact:** Unauthenticated callers can learn internal diagnostics. The parent
identifies FalkorDB/RESP errors, socket failures, invalid bridge-ID conversions
(for example `/api/bridges?ids=abc`), and IBM service errors as exception sources.
Actual leaked details depend on the failure; no deployed response was tested
and no credential disclosure is demonstrated.

#### Root cause and grouping

Public responses expose internal diagnostic text and backend identifiers.
The three exception wrappers and health response share this disclosure root
cause and belong in one remediation issue.

#### Smallest fix

Return `{"error": "internal error"}` from the GET/POST catch-alls, preserving
HTTP 500. Use the same generic error inside the existing MCP tool-result
envelope, preserving `isError: true`. Log exception details server-side only,
without logging credentials. Reduce successful health output to `{"ok": true}`
without graph, game count, or reader details; preserve dependency-failure
signaling if the graph check is retained.

#### Acceptance criteria for the remediation

- [ ] Forced backend/conversion failures return no raw exception type, message,
  host, port, or backend reply through any of the three wrappers.
- [ ] Detailed diagnostics remain available only in appropriate server logs.
- [ ] Health success contains only `{"ok": true}`; failures remain generic.
- [ ] Regression tests preserve HTTP status and MCP envelope behavior.

This scope does not promise that graph identifiers become secret everywhere:
bridge statistics also expose a graph name
([app.py lines 608–610](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L608-L610)).

## Issue 3 — title

`[security][needs-validation] Audit leads to validate`

### Body

Parent audit: https://github.com/Mike-Demo/gaming-multiverse-reasoner/issues/1

These are **unconfirmed leads, with no assigned severity**. Record the evidence
and disposition for each checkbox. Source inspection alone does not establish
the missing data or deployment facts. Do not mark a lead confirmed without
that evidence.

- [ ] **Inline page renders IGDB-derived names/descriptions and watsonx claim
  text through unescaped innerHTML (no CSP backstop).**
  Fingerprint: `dom-xss-innerhtml-data-and-model-text`.
  Claimed root cause: data-derived names, path descriptions, Cypher text, and
  model claims enter `innerHTML` without HTML encoding; no application CSP
  limits execution if markup reaches the page.
  Source pointers:
  [response headers, lines 729–752](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L729-L752),
  [data sinks, lines 1019–1094](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L1019-L1094),
  [claim sink, lines 1115–1123](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L1115-L1123).
  **Exact missing fact from parent:**
  > Whether any string in the frozen IGDB snapshot that flows into these sinks (a game/attribute name, a community_top peer name, or a description component) can contain HTML markup such as an img/onerror payload, or whether Granite claim text in the server-computed browser flow can be induced (via selected games' real graph data) to emit markup — pay…

  The parent ends at `pay…`; request the omitted text from the audit owner.
  Validate snapshot-to-sink or real-graph-to-model provenance and any
  ingress-injected CSP in an authorized environment. Caller-forged narration
  by itself does not establish execution in another user's browser.

- [ ] **FalkorDB master and reader passwords are sent as plaintext RESP AUTH
  over TCP to a default public IP with no TLS.**
  Fingerprint: `falkordb-plaintext-master-credential-transport`.
  Claimed root cause: the application sends graph credentials, including the
  master/default credential, through non-TLS RESP TCP to a host with a public
  IP default.
  Source pointer:
  [socket, AUTH, and credential use, lines 55–147](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L55-L147).
  **Exact missing fact from parent:**
  > The deployed network facts: the actual FALKOR_HOST value in the Code Engine deployment (public vs private/VPC address), whether the Code Engine-to-VSI path stays on private VPC networking, and whether any security group/firewall restricts port 6379 on the VSI to the app's egress — none of these are present in the repository.

  Obtain deployment configuration and route/firewall evidence from the
  operator, including any protected tunnel. Do not collect or publish
  credential values. A public default alone does not prove the deployed route.

- [ ] **Unauthenticated expensive graph/LLM endpoints have no rate, concurrency
  or body-size bound.**
  Fingerprint: `unauthenticated-graph-resource-exhaustion`.
  Claimed root cause: unauthenticated requests can initiate expensive graph
  and model work without in-code request rate, concurrency, batch, or body
  admission controls.
  Source pointers:
  [request bodies and MCP batches, lines 804–820](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L804-L820),
  [threaded server, lines 1725–1727](https://github.com/Mike-Demo/gaming-multiverse-reasoner/blob/2e8d019d2c3818abb33f81d13d645ad3629f127c/app.py#L1725-L1727).
  **Exact missing fact from parent:**
  > Whether the deployed IBM Code Engine ingress/configuration imposes any per-client rate limit, request body-size limit, concurrency limit or max-scale ceiling, and whether the FalkorDB server-side 5,000 ms query timeout asserted in llms.txt (no corresponding config exists in the repo) is actually enforced in deployment — these external caps decide …

  The parent ends at `decide …`; request the omitted text from the audit owner.
  Record effective ingress/scale limits and graph timeout configuration;
  distinguish enforced settings from documentation assertions. Existing
  per-operation limits (for example four game IDs, fourteen narration facts,
  worker and model-output limits) do not establish global admission controls,
  but mean not every dimension of work is unbounded. Do not load-test the
  deployment without explicit authorization.
