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
- **Model calls fall back.** The litellm Router runs groq -> openrouter -> ollama with a per-call
  timeout, 2 retries, a cooldown after failures, and a per-provider concurrency limit. Hosted
  deployments are added only when their key is set. Every LLM span records the model that answered,
  `fallback_used`, and the number of failed attempts before it.
- **Critique is parallel and bounded.** Claims not judged before the critique budget ends are
  struck with the reason `not checked before the critique deadline`, not silently dropped.
- **Abstention.** No supported claim, or under 60 percent supported, gives "insufficient
  evidence". Partial verdicts count as struck.
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
when the run abstains. Gated runs are approved automatically by the harness.

### Results

Two harness runs on 2026-11-09, 20 questions each. In run 1, 11 of 37 web searches failed or were
cut off by the research budget (keyless engines throttle by IP); all 23 Wikipedia calls returned.
`eval/results.json` holds the per-item output of run 1.

| | Run 1 | Run 2 |
|---|---|---|
| Chain | groq `openai/gpt-oss-20b` (fast), `openai/gpt-oss-120b` (strong), Ollama `llama3.2:3b` fallback | Ollama `llama3.2:3b` only |
| Command | `eval.run --max-seconds 30 --pause-seconds 45` | `eval.run --max-seconds 60` |
| LLM calls answered by a fallback | 6 of 60 (Groq tokens-per-minute limit) | n/a |
| Accuracy | 0.35 | 0.40 |
| Answerable accuracy (14 items) | 0.0714 | 0.1429 |
| Answer present in a verified finding (14 items) | 1.0 | not measured |
| Abstention precision / recall | 0.3158 / 1.0 | 0.3333 / 1.0 |
| Mean faithfulness | 1.0 | 0.3138 |
| Latency p50 / p95 | 14.4 s / 20.0 s | 40.0 s / 53.4 s |
| Runs within 30 s | 20 of 20 | 3 of 20 |

What the numbers say. With gpt-oss on Groq, every claim that survived was verified and every run
finished inside 30 s, and the correct answer was in a verified finding for all 14 answerable
questions. But the Analyst wrote one or two claims for one-fact questions, so 13 of 14 fell under
the 3-supported-claim floor and abstained; answerable accuracy measures that floor, not retrieval.
Abstention recall of 1.0 is partly the same effect: the floor abstains on almost everything here.
The local 3B model cannot fit the analyze stage into its budget (one Analyst call took 23 s), so it
needs the 60 s ceiling and still strikes most of its own claims.
