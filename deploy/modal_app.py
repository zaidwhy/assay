"""The public demo on Modal.

    uvx modal deploy deploy/modal_app.py

Needs a Modal secret named `assay-keys` holding GROQ_API_KEY and TAVILY_API_KEY. One container,
because run state and the SQLite file live in the process: a second container would 404 on runs it
did not start. The container stops a few minutes after the last request, which is what keeps a
reviewer demo inside Modal's free credit.
"""

from __future__ import annotations

from pathlib import Path

import modal

REPO = Path(__file__).resolve().parents[1]
MODELS = "/models"

image = (
    modal.Image.debian_slim(python_version="3.12")
    # Exact versions from uv.lock, exported to deploy/requirements.txt.
    .pip_install_from_requirements(str(REPO / "deploy" / "requirements.txt"))
    .env({
        "PYTHONPATH": "/root/src",
        "FASTEMBED_CACHE_PATH": MODELS,
        "ASSAY_USE_OLLAMA": "false",
        "ASSAY_DB_PATH": "/tmp/assay.db",
        "ASSAY_CORS_ORIGINS": "[]",
    })
    .add_local_dir(str(REPO / "src"), "/root/src", copy=True)
    .add_local_dir(str(REPO / "web"), "/root/web", copy=True)
    # Bake both search models into the image so a cold start does not download them.
    .run_commands(
        "python -c \"from fastembed import TextEmbedding; from fastembed.rerank.cross_encoder import TextCrossEncoder; "
        "from assay.settings import Settings; s = Settings(); TextEmbedding(s.embed_model); TextCrossEncoder(s.rerank_model)\""
    )
)

app = modal.App("assay", image=image)


@app.function(
    secrets=[modal.Secret.from_name("assay-keys")],
    cpu=1.0,
    memory=2048,
    max_containers=1,
    scaledown_window=180,
    timeout=900,
)
@modal.concurrent(max_inputs=16)
@modal.asgi_app()
def web():  # noqa: ANN201
    from assay.api import create_app

    return create_app()
