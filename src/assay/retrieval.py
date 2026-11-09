"""Hybrid retrieval: BM25 and dense rankings fused with reciprocal rank fusion, then a cross-encoder rerank."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Protocol

import numpy as np
from rank_bm25 import BM25Okapi

from assay.domain import Chunk, RankedChunk

log = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray: ...


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class FastembedEmbedder:
    def __init__(self, model_name: str, threads: int | None = None) -> None:
        self.model_name = model_name
        self.threads = threads
        self._model = None
        self._lock = threading.Lock()

    def _load(self):  # noqa: ANN202
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding

                log.info("loading embedding model %s", self.model_name)
                self._model = TextEmbedding(self.model_name, threads=self.threads)
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(list(self._load().embed(texts)), dtype=np.float32)


class FastembedReranker:
    def __init__(self, model_name: str, threads: int | None = None) -> None:
        self.model_name = model_name
        self.threads = threads
        self._model = None
        self._lock = threading.Lock()

    def _load(self):  # noqa: ANN202
        with self._lock:
            if self._model is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                log.info("loading rerank model %s", self.model_name)
                self._model = TextCrossEncoder(self.model_name, threads=self.threads)
        return self._model

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self._load().rerank(query, texts)]


def reciprocal_rank_fusion(rankings: list[list[int]], k: int) -> dict[int, float]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, index in enumerate(ranking):
            fused[index] = fused.get(index, 0.0) + 1.0 / (k + rank + 1)
    return fused


class HybridRetriever:
    def __init__(self, embedder: Embedder, reranker: Reranker, *, rrf_k: int, fuse_top: int, rerank_top: int,
                 dense_pool: int = 24, model_input_chars: int = 800,
                 min_source_rerank: float = float("-inf")) -> None:
        self.embedder = embedder
        self.reranker = reranker
        self.rrf_k = rrf_k
        self.fuse_top = fuse_top
        self.rerank_top = rerank_top
        self.dense_pool = dense_pool
        self.model_input_chars = model_input_chars
        self.min_source_rerank = min_source_rerank

    def rank(self, query: str, chunks: list[Chunk]) -> list[RankedChunk]:
        """Top `rerank_top` chunks for the query. CPU-bound; call through `rank_async` from async code."""
        if not chunks:
            return []
        texts = [c.text for c in chunks]
        bm25 = BM25Okapi([tokenize(t) or ["_"] for t in texts])
        lexical = np.asarray(bm25.get_scores(tokenize(query)))
        lexical_order = [int(i) for i in np.argsort(-lexical)]

        # CPU embedding of every chunk costs seconds the 30 s budget does not have. The dense pass
        # covers the lexical top `dense_pool` plus each source's lead chunk, and reads a chunk's
        # opening `model_input_chars`, which is where its topic is usually stated.
        leads = {}
        for i, chunk in enumerate(chunks):
            leads.setdefault(chunk.source_id, i)
        pool = list(dict.fromkeys([*lexical_order[: self.dense_pool], *leads.values()]))
        vectors = self.embedder.embed([query, *(texts[i][: self.model_input_chars] for i in pool)])
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1, norms)
        dense = vectors[1:] @ vectors[0]
        dense_order = [pool[int(i)] for i in np.argsort(-dense)]

        fused = reciprocal_rank_fusion([lexical_order, dense_order], self.rrf_k)
        shortlist = sorted(fused, key=lambda i: fused[i], reverse=True)[: self.fuse_top]
        rerank_scores = self.reranker.score(query, [texts[i][: self.model_input_chars] for i in shortlist])
        reranked = sorted(zip(shortlist, rerank_scores, strict=True), key=lambda pair: pair[1], reverse=True)
        # A source whose best passage the cross-encoder scores below the floor is off topic (a
        # survey paper surfaced by a keyword); none of its chunks may reach the Analyst.
        best_by_source: dict[int, float] = {}
        for index, score in reranked:
            source_id = chunks[index].source_id
            best_by_source[source_id] = max(best_by_source.get(source_id, score), score)
        reranked = [(i, score) for i, score in reranked
                    if best_by_source[chunks[i].source_id] >= self.min_source_rerank]
        return [
            RankedChunk(chunk=chunks[index], rank=position + 1, fused_score=round(fused[index], 6),
                        rerank_score=round(score, 4))
            for position, (index, score) in enumerate(reranked[: self.rerank_top])
        ]

    async def rank_async(self, query: str, chunks: list[Chunk]) -> list[RankedChunk]:
        return await asyncio.to_thread(self.rank, query, chunks)

    def similarity(self, a: str, b: str) -> float:
        vectors = self.embedder.embed([a, b])
        norms = np.linalg.norm(vectors, axis=1)
        if norms[0] == 0 or norms[1] == 0:
            return 0.0
        return float(vectors[0] @ vectors[1] / (norms[0] * norms[1]))
