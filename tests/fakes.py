"""Offline stand-ins: a scripted LLM, in-memory tools, and lexical embedder and reranker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from assay.router import LLMReply, ToolCall
from assay.tools import SourceDoc, Tool

CANBERRA_TEXT = """# Canberra

Canberra is the capital city of Australia. The city was selected as the site of the capital in 1908
as a compromise between Sydney and Melbourne.

## Design

The city was designed by the American architects Walter Burley Griffin and Marion Mahony Griffin
after an international design competition. Construction began in 1913.

## Parliament

Parliament House in Canberra opened in 1988. It is the meeting place of the Parliament of Australia.
"""

GOOD_CLAIMS = [
    ("Canberra is the capital city of Australia.", "Canberra is the capital city of Australia"),
    ("Canberra was chosen as the capital site in 1908.", "selected as the site of the capital in 1908"),
    ("Walter Burley Griffin and Marion Mahony Griffin designed the city.",
     "designed by the American architects Walter Burley Griffin and Marion Mahony Griffin"),
    ("Parliament House opened in 1988.", "Parliament House in Canberra opened in 1988"),
]


def _role(messages: list[dict[str, Any]]) -> str:
    match = re.search(r"ROLE: (\w+)", messages[0]["content"])
    return match.group(1) if match else "unknown"


@dataclass
class ScriptedLLM:
    """Answers by agent role. `claims` is a list of (claim text, quote the critic will return)."""

    claims: list[tuple[str, str]] = field(default_factory=lambda: list(GOOD_CLAIMS))
    high_impact: bool = False
    verdict_follows: bool = True
    failures: dict[str, Exception] = field(default_factory=dict)
    delays: dict[str, float] = field(default_factory=dict)
    blockers: dict[str, asyncio.Event] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def complete(self, tier, messages, *, tools=None, json_mode=False, timeout_s):  # noqa: ANN001
        role = _role(messages)
        self.calls.append(role)
        if role in self.failures:
            raise self.failures[role]
        if role in self.blockers:
            await self.blockers[role].wait()
        if role in self.delays:
            await asyncio.sleep(self.delays[role])
        user = messages[-1]["content"]
        if role == "researcher":
            return self._reply(tool_calls=[ToolCall(name="web_search", arguments={"query": "capital of Australia"}),
                                           ToolCall(name="wikipedia", arguments={"query": "Canberra"})])
        if role == "analyst":
            passage_ids = re.findall(r"^\[(s\d+:c\d+)\]", user, re.M)
            claims = [{"text": text, "chunk_ids": passage_ids[:2]} for text, _ in self.claims]
            return self._reply(text=json.dumps({"claims": claims, "options": ["accept"],
                                                "recommendation": "Treat Canberra as the capital.",
                                                "high_impact": self.high_impact, "impact_reason": "test"}))
        if role == "critic":
            claim = re.search(r"Claim: (.*)", user).group(1)
            quote = dict(self.claims).get(claim, "")
            return self._reply(text=json.dumps({"verdict": "supported", "quote": quote, "reason": "stated"}))
        if role == "reporter":
            return self._reply(text=json.dumps({
                "verdict": "Canberra is the capital of Australia.", "verdict_findings": [1],
                "recommendation": "Use Canberra.", "recommendation_findings": [1, 2],
                "risks": [{"text": "Unsupported risk line", "findings": []}],
                "open_questions": ["None"],
            }))
        if role == "conclusion":
            reason = "it follows" if self.verdict_follows else "the findings are about Thera, not Atlantis"
            return self._reply(text=json.dumps({"follows": self.verdict_follows, "reason": reason}))
        if role == "grader":
            return self._reply(text=json.dumps({"grade": 5, "reason": "answers it"}))
        raise AssertionError(f"unexpected role {role}")

    @staticmethod
    def _reply(text: str = "", tool_calls: list[ToolCall] | None = None) -> LLMReply:
        return LLMReply(text=text, tool_calls=tool_calls or [], deployment="fast:scripted", model="scripted",
                        fallback_used=False, tokens_in=10, tokens_out=5)


def make_tool(name: str, docs: list[SourceDoc], *, hang: bool = False, delay: float = 0.0) -> Tool:
    async def run(args: dict[str, Any]) -> list[SourceDoc]:
        if hang:
            await asyncio.Event().wait()
        if delay:
            await asyncio.sleep(delay)
        return docs

    return Tool(name=name, description=name, parameters={"type": "object", "properties": {}}, run=run)


def canberra_tools(*, hang_wikipedia: bool = False) -> dict[str, Tool]:
    web = [SourceDoc(url="https://example.org/canberra", title="Canberra", text=CANBERRA_TEXT, origin="web")]
    wiki = [SourceDoc(url="https://en.wikipedia.org/wiki/Canberra", title="Canberra (wiki)",
                      text=CANBERRA_TEXT, origin="wikipedia")]
    return {
        "web_search": make_tool("web_search", web),
        "wikipedia": make_tool("wikipedia", wiki, hang=hang_wikipedia),
        "arxiv": make_tool("arxiv", []),
        "fetch": make_tool("fetch", []),
    }


class LexicalEmbedder:
    """Hashed bag of words; similar wording gives similar vectors."""

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), 128), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in re.findall(r"[a-z0-9]+", text.lower()):
                vectors[row, int(hashlib.sha256(token.encode()).hexdigest(), 16) % 128] += 1.0
        return vectors


class OverlapReranker:
    def score(self, query: str, texts: list[str]) -> list[float]:
        wanted = set(re.findall(r"[a-z0-9]+", query.lower()))
        return [float(len(wanted & set(re.findall(r"[a-z0-9]+", t.lower())))) for t in texts]
