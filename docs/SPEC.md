# assay - build spec

Done means: `POST /research` with a question returns, within 30 s, a decision report whose every sentence cites a retrieved passage that a separate check confirmed supports it; sentences that fail the check are struck and shown as struck; when too little survives, the report says "insufficient evidence" instead of deciding.

## Thesis

1. **Data shape.** A run is a tree: question -> sub-queries -> sources -> chunks -> claims -> verdicts. Chunks live for the run (and in session memory for later runs in the same session); verdicts are permanent and auditable.
2. **How it fails in production.** (a) The model writes a fluent sentence the evidence does not say, and a citation marker makes it look checked. (b) A provider rate-limits or a tool hangs, and the 30 s budget is spent waiting.
3. **The opinion.** A claim is only as good as the span that proves it. The Critic checks each claim against the exact cited chunk, not against "the context". Struck claims are a first-class output, not a hidden filter: the reader sees what the system refused to say and why. Below a supported-claim floor (default: no supported claim, or under 60 percent of claims supported; an earlier floor of 3 made one-fact questions abstain), the system abstains.

## Agents (each a small module with one job, one prompt, typed I/O via pydantic)

| Agent | Input | Output | Model tier |
|---|---|---|---|
| Researcher | question, memory | 3-5 sub-queries, tool calls (function calling), gathered sources | fast (llama-3.1-8b-instant) |
| Analyst | question, top reranked chunks (with ids) | draft findings: list of claims, each with chunk ids, plus options/recommendation | strong (llama-3.3-70b-versatile) |
| Critic | each claim + its cited chunk texts | verdict per claim: supported / partial / unsupported, with the quoted span that supports it; source reliability score per source | strong |
| Report Generator | surviving claims, verdicts, sources | structured decision report (markdown + JSON): question, verdict, recommendation, key findings with [n] citations, risks, open questions, struck claims, bibliography | strong |

Orchestration is an explicit async state machine (no agent framework): `planning -> researching -> analyzing -> critiquing -> (awaiting_approval) -> reporting -> done | abstained | failed`. Each transition emits an event.

## Retrieval

- Ingest: web pages (httpx + trafilatura), Wikipedia API, arXiv API, uploaded files: PDF (pypdf), DOCX (python-docx), HTML, Markdown, TXT, CSV. Caps: 20 MB per upload, 200 pages per PDF, 2 MB text per fetched page, 8 s per fetch.
- Long documents: structure-aware chunking (headings / paragraphs, ~350 tokens, 50 overlap), chunk ids `s{source}:c{n}`, char offsets kept so a citation can point at the exact span.
- Hybrid search: BM25 (rank-bm25) + dense (fastembed `BAAI/bge-small-en-v1.5`, local, CPU) fused with reciprocal rank fusion (k=60); top 40 -> cross-encoder rerank (fastembed `Xenova/ms-marco-MiniLM-L-6-v2`) -> top 12 to the Analyst.
- Web search: `ddgs` (DuckDuckGo, keyless) as the default tool; Wikipedia and arXiv as tools; the tool registry is a list of JSON-schema function specs the Researcher calls through the model's function calling.

## Reliability

- LLM router: `litellm.Router` in-process, chain `groq -> openrouter -> ollama (local llama3.2:3b)`, per-call timeout, 2 retries with backoff, cooldown on 429. Every call records which deployment answered and whether it was a fallback.
- Tools: per-tool timeout, 1 retry, failure recorded as a span and the run continues with what it has.
- Deadline: one `Deadline` object created at request start (30 s default). Stages get budgets (plan 3 s, research 10 s, analyze 6 s, critique 6 s, report 4 s); research stops gathering when its budget ends and works with what arrived. Critique runs claims in parallel.
- Unsupported-claim guard (two layers): the Critic's verdict, plus a mechanical check that the quoted supporting span actually occurs in the cited chunk (normalized substring / token overlap >= 0.6). A claim the model "supports" with a quote that is not in the chunk is struck.

## Memory

- Working memory: per-run scratchpad (sub-queries tried, sources seen, notes) passed between agents.
- Session memory: SQLite table of prior questions, verdicts and supported claims per `session_id`; the Researcher retrieves relevant prior findings and the report notes when it reuses one.

## Human in the loop

- A run is high-impact when the request sets `impact: "high"` or a classifier (keyword list + the Analyst's own flag) marks medical, legal, financial, safety or irreversible decisions. High-impact runs stop at `awaiting_approval` with the draft visible; `POST /runs/{id}/approval {decision: approve|reject, note}` releases or rejects. The 30 s budget covers everything up to the approval gate.

## Evaluation (automated, every run)

- Faithfulness: supported claims / total claims (from the Critic plus the span check).
- Citation precision: citations whose chunk supports the claim / citations.
- Relevance: embedding similarity of report to question, plus an LLM 1-5 grade with a one-line reason.
- Quality rubric: structure completeness (all report sections present), source diversity (distinct domains), reliability-weighted evidence.
- Offline harness `eval/`: a labelled set (answerable questions with known answers, and unanswerable or false-premise questions where the correct output is abstention). Prints accuracy, abstention precision/recall, faithfulness, p50/p95 latency. Numbers in the README come only from this harness.

## Observability

- Every agent step, LLM call and tool call is a span: run_id, parent, name, kind, start, latency_ms, tokens_in, tokens_out, model, fallback_used, retries, status, error. Stored in SQLite, streamed live over SSE.
- `GET /runs/{id}/trace`, `GET /metrics` (Prometheus text: request counts, latency histogram, token counters, fallback and failure counters), JSON structured logs via a module logger.

## API (FastAPI)

- `POST /research` {question (8-1000 chars), session_id?, impact?, max_seconds? (<=60)} -> 202 {run_id}
- `GET /runs/{id}` state + report; `GET /runs/{id}/events` SSE; `GET /runs/{id}/trace`; `POST /runs/{id}/approval`
- `POST /documents` multipart upload into a session's corpus
- `GET /health`, `GET /metrics`
- Concurrency: async throughout; a bounded run semaphore (default 8 concurrent runs) and a queue cap (default 32); beyond it 429 with Retry-After. Per-provider concurrency limits in the router.
- Writes: an `ASSAY_API_KEY` header guard when the variable is set; README states the posture.
- SQLite with WAL, `schema_version` table, tables created once at startup.

## Front end

A single page that watches a run live: the question, agents working, sources arriving, claims being assayed, struck claims, final report, trace. Visual direction is chosen by Zaid from rendered previews (`design/previews.html`).

## Rules for this repo

- The name of the assistant vendor that helped build this never appears anywhere in the repo: code, comments, docs, commit messages, file names. No AI attribution of any kind.
- No em dash characters. Python 3.12 via uv. Tests with pytest; tests use a fake LLM and fake tools so they run offline and fast.
