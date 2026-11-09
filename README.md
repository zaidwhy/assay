# assay

A research service that answers a question with a decision report in which every finding is a claim
that was checked against the exact passage it cites.

- **A claim is only as good as the span that proves it.** The Analyst drafts claims with chunk ids.
  The Critic judges each claim against those chunks only, and must quote the span that proves it.
  The quote is then checked mechanically: it has to occur in a cited chunk (normalized substring, or
  a contiguous window sharing at least 60 percent of the quote's tokens). A claim the model calls
  supported with a quote that is not in the chunk is struck.
- **Struck claims are output, not a hidden filter.** The report lists every struck claim with the
  verdict and the reason, so the reader sees what the system refused to say.
- **Below a floor, it abstains.** With no supported claim, or under 60 percent of claims
  supported, the verdict is "insufficient evidence" instead of a decision.
- **The conclusion is checked too.** Each finding is proven against its passage; a separate check
  then confirms the one-sentence verdict follows from those findings without adding a place,
  number or equivalence they do not state. A verdict that fails it is withheld, with the reason.

## Live demo

**https://zaidwhy--assay-web.us-east.modal.run** (Modal, one container, scales to zero: the first
request after idle takes 15-40 s to start). Ask a question, or pick an example; click any claim to
see the sentence in the source that proves it, and open **Trace** for every agent step, model call
and tool call with its latency, tokens, retries and fallbacks.

It runs on free tiers: Groq allows about 8,000 tokens per minute per model, so several questions at
the same moment will see some runs decline for capacity (they say so; they do not guess). Run state
lives in the container, so a run started just before a redeploy is lost.

## What the brief asked for, and where it is

| Requirement | How assay meets it | Verified |
|---|---|---|
| Researcher, Analyst, Critic, Report Generator agents | four modules in `src/assay/agents/`, typed I/O, an explicit state machine in `orchestrator.py` | live runs |
| RAG with hybrid search and reranking | BM25 + dense (bge-small) fused by reciprocal rank fusion, cross-encoder rerank, top 8 to the Analyst | live runs, tests |
| Tool calling: web search, documents, external APIs | the Researcher calls `web_search` (Tavily), `wikipedia`, `arxiv` and `fetch` through function calling; uploads join the corpus | live runs |
| Agent memory across steps | per-run scratchpad passed agent to agent; per-session findings reused by later runs | tests |
| Source attribution and citations | every finding cites bibliography numbers; each verdict carries the exact sentence its quote came from | live runs |
| Automated evaluation | per-run faithfulness, citation precision, relevance (LLM grade), structure, source diversity; a labelled harness in `eval/` | live runs, harness |
| Human approval for high-impact decisions | medical, legal, financial, safety and irreversible questions stop at `awaiting_approval` | live runs (approved in the UI) |
| Observability | a span per agent step, model call and tool call (latency, tokens, model, fallback, retries, status), SSE stream, `GET /runs/{id}/trace`, `/metrics` | live runs |
| Open-source LLMs | open-weight only: gpt-oss-120b, gpt-oss-20b and Qwen 3.8 27B on Groq, Llama 3.2 3B locally. Groq no longer serves Llama, so Llama is the local fallback rather than the lead | live |
| Long documents, many formats | PDF, DOCX, HTML, Markdown, TXT, CSV; offset-keeping chunks; 20 MB / 200-page caps | live: the 77-page Llama 2 paper (13.7 MB) uploaded, then "2 trillion tokens" answered citing it |
| Retries and fallback models | per-call fallback across three Groq models (each has its own rate limit); tools time out, retry once, and fail soft | live: rate-limited calls answered by the backup model, recorded in the trace |
| 30 s end to end | one deadline per run with stage budgets | live: every run measured 10-28 s; harness below |
| No unsupported claims | claim check, mechanical quote check, abstention floor, conclusion check | live, tests, harness |
| Concurrent requests, scalable API | async FastAPI, run semaphore and queue cap, 429 with `Retry-After` | live: 5 simultaneous questions all returned in 22 s (4 declined for model capacity on the free tier) |

## Quickstart

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and either a hosted key or a local
[Ollama](https://ollama.com) with `llama3.2:3b` pulled.

```bash
uv sync
export GROQ_API_KEY=...          # optional; .env.example lists every variable
uv run pytest -q                # offline, a few seconds
uv run assay                    # serves on http://127.0.0.1:8000
```

```bash
curl -s -X POST http://127.0.0.1:8000/research \
  -H 'Content-Type: application/json' \
  -d '{"question": "What is the capital city of Australia and when was it chosen?"}'
# {"run_id": "820f365177d5"}

curl -N http://127.0.0.1:8000/runs/820f365177d5/events     # live event stream
curl -s http://127.0.0.1:8000/runs/820f365177d5            # state, report, verdicts, evaluation
```

The first start downloads the two local ONNX models (embedding and reranker, about 150 MB) and
loads them before serving, so the first run does not pay for it.

### Environment

Variables are read from the process environment; no file is loaded.

| Variable | Effect |
|---|---|
| `GROQ_API_KEY` | adds Groq (`openai/gpt-oss-20b` fast, `openai/gpt-oss-120b` strong, reasoning effort low) first, then Groq `qwen/qwen3.8-27b` as a second deployment with its own rate limit |
| `TAVILY_API_KEY` | web search through Tavily (page text included); without it, keyless DuckDuckGo |
| `ASSAY_OPENROUTER_API_KEY` | optional: adds OpenRouter (`llama-3.1-8b-instruct`, `llama-3.3-70b-instruct`) after Groq. Project-scoped on purpose, so a machine-wide `OPENROUTER_API_KEY` is never spent |
| `ASSAY_USE_OLLAMA` | `true` by default: local `llama3.2:3b` as the last resort |
| `ASSAY_API_KEY` | when set, every write endpoint requires a matching `X-API-Key` header |
| `ASSAY_DB_PATH` | SQLite file, default `assay.db` |

Every setting in `src/assay/settings.py` can be overridden with an `ASSAY_` variable.

## Architecture

```
POST /research -> Admission (8 running, 32 queued) -> Orchestrator state machine
  planning     Researcher (fast tier) plans 3-5 sub-queries as function calls
  researching  web_search (ddgs), wikipedia, and arxiv (scholarly questions only) run in parallel;
               top hits fetched as they arrive
               uploads and prior session findings join as sources; chunked with char offsets
  analyzing    BM25 + dense (bge-small) fused by RRF (k=60), top 40 reranked (MiniLM cross-encoder),
               sources whose best chunk scores under the rerank floor dropped, top 8 to the
               Analyst (strong tier), which drafts claims citing chunk ids
  critiquing   Critic (strong tier) judges every claim in parallel; span check on each quote
  [awaiting_approval]  high-impact runs stop here until POST /runs/{id}/approval
  reporting    Report Generator writes verdict, recommendation, risks from surviving claims only
  -> done | abstained | rejected | failed
```

| Module | Job |
|---|---|
| `orchestrator.py` | the async state machine, admission control, abstention floor |
| `agents/researcher.py` | sub-query planning through function calling; source gathering under the stage budget |
| `agents/analyst.py` | claims with chunk ids from the reranked passages |
| `agents/critic.py` | per-claim verdict, quote, mechanical span check, source reliability |
| `agents/reporter.py` | the report; any line that cites no surviving finding is dropped |
| `router.py` | litellm Router over groq, openrouter, ollama; records the answering deployment |
| `retrieval.py` | BM25, dense, RRF, rerank |
| `ingest.py` | PDF, DOCX, HTML, Markdown, TXT, CSV to text; structure-aware chunking with offsets |
| `tools/` | the four research tools, each a JSON-schema function spec |
| `hitl.py` | high-impact classifier and the approval gate |
| `evaluation.py` | per-run faithfulness, citation precision, relevance, structure, diversity |
| `telemetry.py` | spans, Prometheus metrics, JSON logs |
| `store.py` | SQLite (WAL): runs, events, spans, verdicts, session memory, uploads |

Each agent has one prompt and pydantic models for its input and output. There is no agent framework.

Session memory: when a request carries `session_id`, supported findings are stored per session.
Later runs in the session get the relevant ones (BM25 over prior claims) as a `memory` source the
Analyst may cite, and the report lists any it reused. Uploaded documents join the session's corpus.

## API

| Method and path | Purpose |
|---|---|
| `POST /research` | `{question (8-1000 chars), session_id?, impact? ("normal" or "high"), max_seconds? (<= 60)}` -> 202 `{run_id}`; 429 with `Retry-After` when the queue is full |
| `GET /runs/{id}` | state, report, draft (while awaiting approval), verdicts, evaluation, latency |
| `GET /runs/{id}/events` | Server-Sent Events; schema and examples in [docs/EVENTS.md](docs/EVENTS.md) |
| `GET /runs/{id}/trace` | every span of the run |
| `POST /runs/{id}/approval` | `{decision: "approve" or "reject", note?}`; 409 unless the run is awaiting approval |
| `POST /documents` | multipart `file` and `session_id`; 413 above 20 MB, 422 for unsupported or unreadable files |
| `GET /health` | database status, schema version, the active LLM chain, running and queued counts |
| `GET /metrics` | Prometheus text: requests, run latency histogram, tokens, fallbacks, span failures, admission rejections |

Security posture: the service binds to 127.0.0.1. Without `ASSAY_API_KEY` the write endpoints are
open to anything that can reach that port; set it before exposing the service. Read endpoints are
never guarded, so run ids are the only protection for reports. CORS allows only the origins in
`ASSAY_CORS_ORIGINS`. The fetch tool refuses non-public addresses, because page text can steer the
model toward internal URLs.

## Reliability design

- **One deadline per run.** Created when the request arrives (30 s default, `max_seconds` up to
  60). Stage budgets: plan 3 s, research 10 s, analyze 6 s, critique 6 s, report 4 s, scaled for
  longer runs. A stage may also use slack an earlier stage left, never more than the run has left.
- **Research works with what arrived.** Tool calls and page fetches run concurrently; when the
  research budget ends, unfinished calls are cancelled (their spans say `cancelled`) and the run
  continues with the sources it has. A search snippet whose page could not be fetched still counts.
- **Tools fail soft.** Each tool call has a timeout and one retry; a failure becomes a span with
  `status: error` and the run goes on.
- **Model calls fall back.** The litellm Router runs Groq gpt-oss -> Groq Qwen -> OpenRouter (only
  with `ASSAY_OPENROUTER_API_KEY`) -> Ollama, with a per-call timeout, a cooldown after failures and
  a per-provider concurrency limit. A rate limit or a timeout goes straight to the next model,
  because it will not clear inside a 30 s run; other errors retry twice. Every LLM span records the
  model that answered, `fallback_used`, and the number of failed attempts before it.
- **Critique is parallel and bounded.** Claims not judged before the critique budget ends are
  struck with the reason `not checked before the critique deadline`, not silently dropped.
- **Abstention names its cause.** No supported claim, or under 60 percent supported, gives
  "insufficient evidence". Partial verdicts count as struck. When the Analyst call itself failed
  (a rate limit, for example), the report says model capacity ran out instead of blaming the
  evidence.
- **Approval gate.** A run is high impact when the request says `impact: "high"`, the question hits
  the medical, legal, financial, safety or irreversible keyword list, or the Analyst flags it. It
  stops at `awaiting_approval` with the draft visible. The 30 s budget covers everything up to the
  gate; after approval the report gets its own 4 s. A run that abstains is never gated.
- **Admission.** At most 8 runs execute and 32 more wait; beyond that `POST /research` returns 429
  with `Retry-After`. A run waiting at the approval gate does not hold an execution slot.
- **Off-topic sources are dropped.** arXiv is offered to the Researcher only when the question
  reads as scholarly, and a source whose best chunk the cross-encoder scores below
  `ASSAY_MIN_SOURCE_RERANK` (default -4) contributes nothing to the Analyst.
- **Provider slots queue, not fail.** Calls wait for one of `ASSAY_PROVIDER_MAX_PARALLEL` slots
  before reaching the router, so a run with many claims does not turn a full deployment into
  struck claims. A Groq tokens-per-minute limit is a 429 like any other: the call falls back.
- **Latency cost of local retrieval.** CPU embedding of every chunk takes seconds. The dense pass
  covers the BM25 top 24 plus each source's first chunk, and the dense and rerank models read each
  chunk's first 800 characters (`ASSAY_DENSE_POOL`, `ASSAY_MODEL_INPUT_CHARS`).

## Evaluation

Every run is scored automatically and the scores are in `GET /runs/{id}` under `evaluation`:
faithfulness (supported / total claims), citation precision (citations whose chunk contains the
verified quote / citations), relevance (embedding similarity of report to question, plus an LLM
grade from 1 to 5 with a reason), structure completeness, source diversity (distinct domains among
cited sources) and reliability-weighted evidence.

The offline harness runs a labelled set through the real pipeline: 14 questions with short known
answers and 6 unanswerable or false-premise questions where the correct output is abstention.

```bash
uv run python -m eval.run                    # 30 s budget per question
uv run python -m eval.run --max-seconds 60 --ids a01,u02
```

It prints accuracy, abstention precision and recall, mean faithfulness and p50/p95 latency, and
writes every item to `eval/results.json`. An answerable item is correct when the run does not abstain
and a known answer appears in the verdict or a surviving finding; an unanswerable item is correct
when the run abstains or its verdict rejects the premise in one of the item's labelled phrases
("Venus has no natural satellites"). Gated runs are approved automatically by the harness.

### Results

Two harness runs, 20 questions each, 30 s budget, paced so each model's per-minute quota refills.
They are reported as they came out, including the second, which ran with the free tier's daily
quotas nearly spent.

| | Run A, 2026-11-09 22:10 IST | Run B, 2026-11-10 00:25 IST |
|---|---|---|
| Chain | Groq `gpt-oss-20b` / `gpt-oss-120b`, OpenRouter Llama 3.x fallback | Groq `gpt-oss-20b` / `gpt-oss-120b` / `qwen3.8-27b` |
| Code | before the conclusion check | conclusion check, Groq backup model, top 8 passages |
| Runs within 30 s | 20 of 20 | 20 of 20 |
| Latency p50 / p95 | 16.0 s / 19.8 s | 15.5 s / 22.5 s |
| Answerable: answer in a verified finding | 12 of 14 | 12 of 14 |
| Answerable: correct (answered, not abstained) | 12 of 14 | 5 of 14 |
| Unanswerable or false premise handled | 3 of 6 (scored on abstention only) | 6 of 6 |
| Mean faithfulness | 0.94 | 0.39 |

What happened between them. Run A exposed the Atlantis failure: a verified finding about a Bronze
Age city on Thera became "the population of Atlantis is about 30,000". The conclusion check that
followed is why run B handles all 6 false-premise questions. Run B also exposed two bugs, both
fixed after it: with the daily quota of `gpt-oss-20b` spent and Qwen over its per-minute cap, 58
Critic calls failed for capacity, and those claims were (1) labelled "critic reply was not a valid
verdict" and (2) counted against the 60 percent share, so runs whose answer was already verified
abstained. Unchecked claims are now marked `checked: false`, kept out of the report, and left out of
the share. The fixes target the gap between 12 answers found and 5 given; whether they close it
is not measured yet, and a rerun on a fresh daily quota is the next measurement. `eval/results.json` holds run B per item.
