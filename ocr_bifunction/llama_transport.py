"""Shared llama-swap transport — the ONE HTTP mechanic every SLM / VLM slot POSTs through.

Five thin clients talk to the shared llama-swap proxy: the granite generator (reference edges),
the LightOnOCR VLM engine, the template suggester, the field namer, and the GGUF embedding
retriever. Each builds its OWN prompt/payload and parses its OWN reply — but the wire mechanic
was copied five times: resolve the base URL (arg, else `LLAMA_SWAP_URL`, else the local default),
POST JSON to a llama-swap path, and turn an HTTP / connection failure into a RuntimeError that
names the server. This module owns exactly that mechanic; the endpoint SHAPE (`/v1/chat/completions`
vs `/completion` vs `/v1/embeddings`) and the reply parsing stay with each caller.

It owns no process and no state: llama-swap lazy-loads/TTL-unloads the models (nothing to start
or stop). Env override for every caller: `LLAMA_SWAP_URL` (each caller keeps its own model-key env).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# The shared llama-swap proxy (OpenAI-compatible). Base URL WITHOUT a /v1 suffix — each caller
# appends its own path. IT / the sibling SLM projects override via the LLAMA_SWAP_URL env.
DEFAULT_LLAMA_SWAP_URL = "http://127.0.0.1:8080"


def resolve_base_url(base_url: str | None = None) -> str:
    """The llama-swap base URL with no trailing slash: the arg, else `LLAMA_SWAP_URL`, else default."""
    return (
        base_url or os.environ.get("LLAMA_SWAP_URL", DEFAULT_LLAMA_SWAP_URL)
    ).rstrip("/")


def post_json(
    base_url: str,
    path: str,
    body: dict,
    *,
    timeout: float,
    server_label: str,
) -> dict:
    """POST `body` as JSON to `{base_url}{path}` on llama-swap and return the parsed JSON reply.

    `server_label` names the server in an HTTP error (`"{server_label} server HTTP {code}: …"`);
    an unreachable proxy raises the shared `"shared llama-swap unreachable at {base_url} …"` — the
    exact two failures every caller surfaced by hand. The first call may block while llama-swap
    lazy-loads the model on CPU, so callers pass a generous timeout."""
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(
            f"{server_label} server HTTP {error.code}: {detail}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"shared llama-swap unreachable at {base_url} "
            f"(is it running?): {error.reason}"
        ) from error
