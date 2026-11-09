"""HTTP API. `create_app` takes its collaborators as arguments so tests run fully offline."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Path, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from assay.domain import TERMINAL_STATES, RunState
from assay.events import EventBus
from assay.hitl import ApprovalDecision, ApprovalGate
from assay.ingest import IngestError, extract_text
from assay.orchestrator import Orchestrator, QueueFull, ResearchRequest, run_view
from assay.retrieval import FastembedEmbedder, FastembedReranker, HybridRetriever
from assay.router import LLMBackend, RouterLLM
from assay.settings import Settings, get_settings
from assay.store import SCHEMA_VERSION, Store
from assay.telemetry import Metrics, configure_logging
from assay.tools import Tool
from assay.tools.registry import default_tools

log = logging.getLogger(__name__)

RunId = Annotated[str, Path(pattern=r"^[0-9a-f]{12}$")]
SSE_KEEPALIVE_S = 15.0
UPLOAD_READ_BYTES = 1024 * 1024


def create_app(
    settings: Settings | None = None,
    *,
    llm: LLMBackend | None = None,
    tools: dict[str, Tool] | None = None,
    retriever: HybridRetriever | None = None,
    grade_with_llm: bool = True,
    warm_models: bool = True,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.db_path)
        active_retriever = retriever or HybridRetriever(
            FastembedEmbedder(settings.embed_model), FastembedReranker(settings.rerank_model),
            rrf_k=settings.rrf_k, fuse_top=settings.fuse_top, rerank_top=settings.rerank_top,
            dense_pool=settings.dense_pool, model_input_chars=settings.model_input_chars,
            min_source_rerank=settings.min_source_rerank,
        )
        if warm_models:
            # Loading the ONNX models takes seconds; pay it at startup, not inside the first run's budget.
            await asyncio.to_thread(active_retriever.similarity, "warm", "up")
            await asyncio.to_thread(active_retriever.reranker.score, "warm", ["up"])
        active_llm = llm or RouterLLM(settings)
        if warm_models and isinstance(active_llm, RouterLLM):
            await active_llm.warm()
        bus = EventBus(store)
        app.state.store = store
        app.state.bus = bus
        app.state.metrics = metrics
        app.state.llm = active_llm
        app.state.orchestrator = Orchestrator(
            store=store, bus=bus, settings=settings, llm=active_llm, tools=tools or default_tools(settings),
            retriever=active_retriever, metrics=metrics, gate=ApprovalGate(), grade_with_llm=grade_with_llm,
        )
        yield
        await app.state.orchestrator.shutdown()
        store.close()

    metrics = Metrics()
    app = FastAPI(title="assay", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key", "Last-Event-ID"],
    )

    @app.middleware("http")
    async def count_requests(request: Request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        route = request.scope.get("route")
        metrics.requests.labels(route=getattr(route, "path", "unmatched"), status=str(response.status_code)).inc()
        return response

    def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
        if settings.api_key and not (x_api_key and secrets.compare_digest(x_api_key, settings.api_key)):
            raise HTTPException(status_code=401, detail="X-API-Key header missing or wrong")

    def orchestrator(request: Request) -> Orchestrator:
        return request.app.state.orchestrator

    def store_of(request: Request) -> Store:
        return request.app.state.store

    @app.post("/research", status_code=202, dependencies=[Depends(require_api_key)])
    async def start_research(body: ResearchRequest, request: Request) -> dict[str, str]:
        try:
            run_id = orchestrator(request).submit(body)
        except QueueFull as exc:
            return JSONResponse(status_code=429, content={"detail": str(exc)},
                                headers={"Retry-After": str(settings.queue_retry_after_s)})
        return {"run_id": run_id}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: RunId, request: Request) -> dict:
        view = run_view(store_of(request), run_id)
        if view is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return view

    @app.get("/runs/{run_id}/trace")
    async def get_trace(run_id: RunId, request: Request) -> dict:
        store = store_of(request)
        if store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return {"run_id": run_id, "spans": store.spans_for_run(run_id)}

    @app.get("/runs/{run_id}/events")
    async def stream_events(run_id: RunId, request: Request,
                            last_event_id: Annotated[int | None, Header(ge=0)] = None) -> StreamingResponse:
        store = store_of(request)
        if store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        bus: EventBus = request.app.state.bus

        async def frames() -> AsyncIterator[str]:
            after = last_event_id or 0
            while True:
                waiter = bus.subscribe(run_id)
                try:
                    for event in store.events_after(run_id, after):
                        after = event["seq"]
                        envelope = {"type": event["type"], "run_id": run_id, "seq": event["seq"],
                                    "ts": event["ts"], "data": event["data"]}
                        yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(envelope)}\n\n"
                    row = store.get_run(run_id)
                    if row is None or row["state"] in TERMINAL_STATES:
                        return
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(waiter.wait(), timeout=SSE_KEEPALIVE_S)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                finally:
                    bus.unsubscribe(run_id, waiter)

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/runs/{run_id}/approval", dependencies=[Depends(require_api_key)])
    async def approve(run_id: RunId, body: ApprovalDecision, request: Request) -> dict[str, str]:
        row = store_of(request).get_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        if row["state"] != RunState.awaiting_approval or not orchestrator(request).gate.decide(run_id, body):
            raise HTTPException(status_code=409, detail=f"run {run_id} is {row['state']}, not awaiting_approval")
        return {"run_id": run_id, "decision": body.decision}

    @app.post("/documents", status_code=201, dependencies=[Depends(require_api_key)])
    async def upload_document(
        request: Request,
        file: Annotated[UploadFile, File()],
        session_id: Annotated[str, Form(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")],
    ) -> dict:
        name = (file.filename or "upload")[:200]
        payload = bytearray()
        while piece := await file.read(UPLOAD_READ_BYTES):
            payload.extend(piece)
            if len(payload) > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"file {name!r} exceeds the upload limit of {settings.max_upload_bytes} bytes "
                           f"({settings.max_upload_bytes // (1024 * 1024)} MB)",
                )
        try:
            text = await asyncio.to_thread(extract_text, name, bytes(payload), settings)
        except IngestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        document_id = store_of(request).add_document(session_id, name, file.content_type or "", len(payload), text)
        return {"document_id": document_id, "session_id": session_id, "name": name, "chars": len(text)}

    @app.get("/health")
    async def health(request: Request) -> dict:
        llm = request.app.state.llm
        return {
            "status": "ok" if store_of(request).ping() else "degraded",
            "schema_version": SCHEMA_VERSION,
            "llm_chain": llm.describe() if hasattr(llm, "describe") else None,
            "running": orchestrator(request).admission.running,
            "queued": orchestrator(request).admission.waiting,
        }

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000, log_config=None)


if __name__ == "__main__":
    main()
