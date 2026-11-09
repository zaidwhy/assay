FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    FASTEMBED_CACHE_PATH=/app/models PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY web ./web
RUN uv sync --frozen --no-dev

# Bake the embedding and rerank models into the image so a cold start does not download them.
RUN uv run --no-sync python -c "from fastembed import TextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; \
from assay.settings import Settings; s = Settings(); TextEmbedding(s.embed_model); TextCrossEncoder(s.rerank_model)"

RUN useradd --create-home assay && mkdir -p /app/data && chown -R assay /app/data /app/models
USER assay

# No Ollama in the container: the chain is Groq's three models. The SQLite file lives in /app/data.
ENV ASSAY_HOST=0.0.0.0 ASSAY_USE_OLLAMA=false ASSAY_DB_PATH=/app/data/assay.db ASSAY_CORS_ORIGINS=[]
EXPOSE 8000
CMD ["sh", "-c", "ASSAY_PORT=${PORT:-8000} exec uv run --no-sync assay"]
