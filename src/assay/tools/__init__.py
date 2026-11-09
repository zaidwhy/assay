"""Research tools. Each is a JSON-schema function spec the Researcher calls through function calling.

A tool returns `SourceDoc`s. Tools never raise past their own boundary for expected network
trouble; the orchestrator wraps every call in a timeout, one retry and a span.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel

from assay.domain import SourceOrigin


class SourceDoc(BaseModel):
    url: str
    title: str
    text: str
    origin: SourceOrigin
    # True when only a search snippet arrived; the orchestrator may fetch the full page.
    snippet_only: bool = False


ToolRunner = Callable[[dict[str, Any]], Awaitable[list[SourceDoc]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: ToolRunner

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def domain_of(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.") or "local"


_TRUSTED_SUFFIXES = (".gov", ".edu", ".int", ".ac.uk", ".gov.uk", ".nih.gov")
_REFERENCE_DOMAINS = {
    "en.wikipedia.org": 0.7,
    "arxiv.org": 0.75,
    "britannica.com": 0.8,
    "nature.com": 0.85,
    "who.int": 0.85,
    "nasa.gov": 0.85,
    "reuters.com": 0.75,
    "apnews.com": 0.75,
    "bbc.com": 0.7,
}


def prior_reliability(url: str, origin: SourceOrigin) -> float:
    """A starting reliability from the domain alone; the Critic refines it with support rates."""
    if origin == "upload":
        return 0.7
    domain = domain_of(url)
    if domain in _REFERENCE_DOMAINS:
        return _REFERENCE_DOMAINS[domain]
    if domain.endswith(_TRUSTED_SUFFIXES):
        return 0.8
    return 0.5
