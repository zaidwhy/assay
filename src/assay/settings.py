"""Runtime configuration, read once from ASSAY_* environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ASSAY_", extra="ignore")

    db_path: Path = Path("assay.db")
    # When set, every write endpoint requires the X-API-Key header to match.
    api_key: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])

    # Admission control.
    max_concurrent_runs: int = Field(default=8, ge=1, le=256)
    queue_cap: int = Field(default=32, ge=0, le=4096)
    queue_retry_after_s: int = Field(default=5, ge=1, le=600)

    # Deadline and stage budgets in seconds, for the default 30 s run. Longer runs scale them.
    default_deadline_s: float = Field(default=30.0, gt=0, le=60)
    max_deadline_s: float = Field(default=60.0, gt=0, le=600)
    budget_plan_s: float = 3.0
    budget_research_s: float = 10.0
    budget_analyze_s: float = 6.0
    budget_critique_s: float = 6.0
    budget_report_s: float = 4.0

    # Abstention floor: abstain below this many supported claims or this supported share.
    # One claim proven by its exact quoted span answers a one-fact question; the share is what
    # catches a draft that is mostly wrong. A floor of 3 abstained on 13 of 14 answerable eval
    # questions whose answer was already in a verified claim.
    min_supported_claims: int = Field(default=1, ge=0)
    min_supported_ratio: float = Field(default=0.6, ge=0, le=1)
    span_overlap_threshold: float = Field(default=0.6, ge=0, le=1)

    # Ingest caps.
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pdf_pages: int = 200
    max_fetch_bytes: int = 2 * 1024 * 1024
    fetch_timeout_s: float = 8.0
    tool_timeout_s: float = 8.0
    max_sources_per_run: int = Field(default=12, ge=1, le=100)
    # ddgs backend list ("auto" tries its enabled engines in turn). Keyless engines throttle by IP.
    search_backend: str = "auto"
    pages_to_fetch: int = Field(default=4, ge=0, le=20)
    # Research stops early once this many sources are in and this many seconds have passed:
    # a hung search otherwise holds the stage to its full budget for evidence it no longer needs.
    research_enough_sources: int = Field(default=6, ge=1, le=100)
    research_settle_s: float = Field(default=5.0, ge=0)

    # Chunking and retrieval.
    chunk_tokens: int = Field(default=350, ge=50, le=4000)
    chunk_overlap: int = Field(default=50, ge=0, le=1000)
    rrf_k: int = 60
    fuse_top: int = 40
    rerank_top: int = 12
    dense_pool: int = Field(default=24, ge=1, le=1000)
    model_input_chars: int = Field(default=800, ge=100, le=20_000)
    # Cross-encoder logit floor for a source's best chunk. MiniLM scores on-topic passages
    # around +5 and unrelated ones around -10.
    min_source_rerank: float = -4.0
    embed_model: str = "BAAI/bge-small-en-v1.5"
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"

    # LLM chain. A hosted deployment is used only when its key variable is present.
    groq_fast_model: str = "groq/openai/gpt-oss-20b"
    groq_strong_model: str = "groq/openai/gpt-oss-120b"
    # Groq rate-limits per model, so a second Groq model is independent capacity when the first hits 429.
    groq_backup_model: str = "groq/qwen/qwen3.8-27b"
    openrouter_fast_model: str = "openrouter/meta-llama/llama-3.1-8b-instruct"
    openrouter_strong_model: str = "openrouter/meta-llama/llama-3.3-70b-instruct"
    ollama_model: str = "ollama_chat/llama3.2:3b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_num_ctx: int = 8192
    use_ollama: bool = True
    llm_timeout_s: float = 20.0
    llm_num_retries: int = Field(default=2, ge=0, le=5)
    llm_cooldown_s: float = 30.0
    provider_max_parallel: int = Field(default=8, ge=1, le=256)

    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
