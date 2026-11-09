# Run event stream

`GET /runs/{run_id}/events` is a Server-Sent Events stream. It replays every event of the run from
the start, then follows live events until the run reaches a terminal state (`done`, `abstained`,
`rejected`, `failed`), then closes. A run held at `awaiting_approval` keeps the stream open; a
`: keepalive` comment is sent every 15 s while nothing happens.

To resume after a disconnect, reconnect with the `Last-Event-ID` header set to the last `seq` you
processed; only later events are sent. Browsers' `EventSource` does this automatically.

## Frame

Each event is one SSE frame. The SSE `event:` field carries the type, so
`source.addEventListener("claim.verdict", ...)` works, and `data:` holds the full envelope:

```
id: 53
event: claim.verdict
data: {"type": "claim.verdict", "run_id": "820f365177d5", "seq": 53, "ts": "2026-11-09T15:42:44.257+00:00", "data": {...}}
```

| Envelope field | Type | Meaning |
|---|---|---|
| `type` | string | one of the eight types below |
| `run_id` | string | 12 hex characters |
| `seq` | integer | 1-based, strictly increasing within a run; also the SSE `id` |
| `ts` | string | ISO 8601 UTC, millisecond precision, when the event was persisted |
| `data` | object | type-specific payload, documented below |

Events arrive in causal order: a claim's `claim.drafted` comes before its `claim.verdict`, every
`chunk.ranked` comes before the first `claim.drafted`, and `report.ready` comes before the terminal
`run.state`. `claim.verdict` events arrive in completion order, not claim order. `span` events are
interleaved everywhere, each sent when its span closes.

Examples with run id `820f365177d5` are copied from a real run (`report` and long strings trimmed).
The `awaiting_approval` and struck-verdict examples show the shape of cases that run did not hit;
their ids and values are illustrative.

## run.state

Every state transition. `previous` is `null` only for the first event (`queued`).

States in order: `queued`, `planning`, `researching`, `analyzing`, `critiquing`, then
`awaiting_approval` (high-impact runs only), `reporting`, and one terminal state: `done`,
`abstained` (too few supported claims, the report says "insufficient evidence"), `rejected` (a
reviewer rejected it at approval) or `failed` (an internal error; `detail.reason` says what).

`detail` is present on some transitions: `awaiting_approval` carries `{"reasons": [...]}`,
`rejected` carries `{"note": "..."}`, `failed` carries `{"reason": "..."}`.

```json
{"type": "run.state", "run_id": "820f365177d5", "seq": 2, "ts": "2026-11-09T15:42:24.780+00:00",
 "data": {"state": "planning", "previous": "queued", "elapsed_ms": 0}}
```

```json
{"type": "run.state", "run_id": "3b0c1f9a2d4e", "seq": 61, "ts": "2026-11-09T16:02:10.114+00:00",
 "data": {"state": "awaiting_approval", "previous": "critiquing", "elapsed_ms": 19870,
          "detail": {"reasons": ["medical keyword 'dose'"]}}}
```

## query.planned

The Researcher's plan: distinct sub-queries and the tool calls that run them. `planned_by_model`
is false when the model planned nothing usable and the question itself was searched.
`reused_findings` counts prior findings from the same session handed to this run.

```json
{"type": "query.planned", "run_id": "820f365177d5", "seq": 5, "ts": "2026-11-09T15:42:26.125+00:00",
 "data": {"sub_queries": ["capital city of Australia chosen date", "Capital of Australia Wikipedia",
                          "capital city Australia chosen"],
          "tool_calls": [{"tool": "web_search", "arguments": {"query": "capital city of Australia chosen date"}},
                         {"tool": "wikipedia", "arguments": {"query": "Capital of Australia Wikipedia"}}],
          "planned_by_model": true, "reused_findings": 0}}
```

## source.found

A source entered the run's corpus. `reliability` is the domain prior in [0, 1]; the final,
support-adjusted score is in the report's bibliography. `origin` is one of `web`, `wikipedia`,
`arxiv`, `upload`, `memory`. Uploaded files have `domain` `upload` and an `upload://` URL; findings
reused from earlier runs in the same session arrive as one `memory` source with a `memory://` URL.

```json
{"type": "source.found", "run_id": "820f365177d5", "seq": 8, "ts": "2026-11-09T15:42:28.112+00:00",
 "data": {"source_id": 1, "url": "https://en.wikipedia.org/wiki/Australian_Capital_Territory",
          "title": "Australian Capital Territory", "domain": "en.wikipedia.org", "reliability": 0.7,
          "origin": "wikipedia"}}
```

## chunk.ranked

One per chunk sent to the Analyst (top 12 after rerank), in rank order. `chunk_id` is
`s{source_id}:c{n}`. `start` and `end` are character offsets into the source text, end exclusive.
`rerank_score` is the cross-encoder logit (higher is better, unbounded); `fused_score` is the
reciprocal rank fusion score.

```json
{"type": "chunk.ranked", "run_id": "820f365177d5", "seq": 30, "ts": "2026-11-09T15:42:37.154+00:00",
 "data": {"chunk_id": "s9:c0", "source_id": 9, "rank": 1, "fused_score": 0.032787, "rerank_score": 8.1745,
          "heading": "", "start": 0, "end": 2187,
          "preview": "The capital city of Australia is Canberra, not Sydney or Melbourne. Canberra is "}}
```

## claim.drafted

A claim the Analyst wrote, with the chunk ids it cites. Claim ids are `k1`, `k2`, ... per run.
`chunk_ids` is what the model wrote; ids that were not among the ranked chunks are ignored by the
Critic, and a claim left with none is struck.

```json
{"type": "claim.drafted", "run_id": "820f365177d5", "seq": 44, "ts": "2026-11-09T15:42:42.526+00:00",
 "data": {"id": "k1", "text": "The capital city of Australia is Canberra.", "chunk_ids": ["s9:c0", "s8:c0", "s6:c0"]}}
```

## claim.verdict

The Critic's verdict on one claim. `verdict` is the model's call (`supported`, `partial`,
`unsupported`); `span_found` is the mechanical check that `quote` occurs in a cited chunk;
`struck` is the outcome: a claim survives only when `verdict` is `supported` and `span_found` is
true. `reason` explains a strike, for example `quoted span does not occur in the cited passage` or
`not checked before the critique deadline`.

`passage` is the sentence in the cited chunks that best matches `quote` (`match`), with up to 260
characters of context on each side (`before`, `after`) and the `chunk_id` it came from, so a reader
can see the words the verdict rests on. It is `null` when there is no quote or no sentence shares a
word with it.

```json
"passage": {"chunk_id": "s9:c0", "before": "...New South Wales in 1911. ",
            "match": "Canberra was selected as the site of the capital in 1908.", "after": " The city..."}
```

```json
{"type": "claim.verdict", "run_id": "820f365177d5", "seq": 53, "ts": "2026-11-09T15:42:44.257+00:00",
 "data": {"id": "k7", "verdict": "supported",
          "quote": "The Australian Capital Territory (ACT) was carved out of New South Wales in 1911",
          "reason": "The quote directly states the claim", "span_found": true, "struck": false}}
```

```json
{"type": "claim.verdict", "run_id": "a41d9e0b77c2", "seq": 48, "ts": "2026-11-09T16:10:02.551+00:00",
 "data": {"id": "k3", "verdict": "supported", "quote": "Sydney was made the capital in 1901",
          "reason": "quoted span does not occur in the cited passage", "span_found": false, "struck": true}}
```

## report.ready

The final report, sent once. `report` has the same shape as `report` in `GET /runs/{id}`:

| Field | Type |
|---|---|
| `question` | string |
| `verdict` | string; `"insufficient evidence"` when abstained |
| `verdict_citations` | list of bibliography numbers |
| `abstained` | bool |
| `recommendation` | `{"text", "citations"}` or null |
| `key_findings` | list of `{"n", "claim_id", "text", "citations"}` |
| `risks` | list of `{"text", "citations"}` |
| `open_questions` | list of strings |
| `struck_claims` | list of `{"claim_id", "text", "chunk_ids", "verdict", "quote", "reason", "span_found", "struck"}` |
| `bibliography` | list of `{"n", "source_id", "title", "url", "domain", "reliability"}` |
| `reused_prior_findings` | list of strings |
| `markdown` | the same report rendered as Markdown |

```json
{"type": "report.ready", "run_id": "820f365177d5", "seq": 70, "ts": "2026-11-09T15:42:47.898+00:00",
 "data": {"verdict": "The capital city of Australia is Canberra, which was chosen in 1908", "abstained": false,
          "report": {"question": "What is the capital city of Australia and when was it chosen?",
                     "verdict": "The capital city of Australia is Canberra, which was chosen in 1908",
                     "verdict_citations": [1, 2, 3, 6], "abstained": false,
                     "recommendation": {"text": "...", "citations": [1, 2]},
                     "key_findings": [{"n": 1, "claim_id": "k1", "text": "The capital city of Australia is Canberra.",
                                       "citations": [1, 2, 3]}],
                     "risks": [], "open_questions": ["..."], "struck_claims": [],
                     "bibliography": [{"n": 1, "source_id": 9, "title": "What Is the Capital City of Australia?",
                                       "url": "https://www.insideaustraliatravel.com/faqs/australia-capital-city",
                                       "domain": "insideaustraliatravel.com", "reliability": 0.75}],
                     "reused_prior_findings": [], "markdown": "..."},
          "markdown": "# What is the capital city of Australia and when was it chosen?\n\n**Verdict:** ..."}}
```

## span

One telemetry span, sent when it closes. `kind` is `run`, `stage`, `agent`, `llm` or `tool`.
`parent_id` links spans into a tree (null for the `run` span). `status` is `ok`, `error`,
`timeout` or `cancelled` (a tool still running when its stage budget ended). On `llm` spans,
`model` is the model that answered, `fallback_used` is true when a deployment other than the
first in the chain answered, `retries` counts failed attempts before the answer, and
`attrs.deployment` names the deployment (for example `strong:openrouter`). On `tool` spans,
`attrs.arguments` holds the call arguments and `attrs.documents` the number of documents returned.

```json
{"type": "span", "run_id": "820f365177d5", "seq": 3, "ts": "2026-11-09T15:42:26.121+00:00",
 "data": {"span_id": "e55c282cf0204db7", "run_id": "820f365177d5", "parent_id": "2959c404b349430a",
          "name": "researcher.plan", "kind": "llm", "start": "2026-11-09T15:42:24.782+00:00",
          "latency_ms": 1336.5, "tokens_in": 499, "tokens_out": 50,
          "model": "meta-llama/llama-3.1-8b-instruct", "fallback_used": false, "retries": 0,
          "status": "ok", "error": null, "attrs": {"tier": "fast", "deployment": "fast:openrouter"}}}
```

The same spans, as a list, are available after the fact from `GET /runs/{run_id}/trace`.
