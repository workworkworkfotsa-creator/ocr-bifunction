"""Generative slot — the LLM tier that extracts reference EDGES from contract articles.

Jettisonable adapter, SAME shape as `GgufEmbeddingRetriever` in `rag.py`: a llama-server
child owns one GGUF (granite-4.0-h-tiny), started lazily on first call and stopped on
close() — use it as a context manager so the process never leaks. Following that pattern
(one owned child process) rather than llama-swap keeps close() reliable: terminating the
server kills the model, with no orphaned grandchild to `taskkill`.

The one job today is REFERENCE EXTRACTION. Given one article's text, the model returns the
links it creates to other documents/articles/annexes as DIRECTIONAL edges (ancien/nouveau).
The prompt is the one PROVEN on the real Avenant Article 2 (2026-07-01): a free `cible/par`
schema was unstable (both the label and the direction flipped between runs), so it is pinned
by directional fields + an exact enum + a one-shot example. The Generator slot (process +
HTTP) is generic; the prompt and the JSON parsing are pure functions on top, so parsing is
testable without a server.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# The llama.cpp build + granite GGUF this concept was proven on (2026-07-01, via llama-swap
# — the same binary and flags, launched directly here). IT overrides via env.
_DEFAULT_GENERATION_BINARY = (
    r"C:\Users\filipeparente\Tools\llamacpp\b9542\llama-server.exe"
)
_DEFAULT_GENERATION_MODEL = (
    r"C:\Users\filipeparente\Models_gguf\granite-4.0-h-tiny-Q4_K_M.gguf"
)
# Mirror the proven llama-swap entry for this model: -c 16384 -t 3 -fa on -ngl 0 --mmap --jinja.
_GENERATION_CONTEXT_SIZE = 16384
_GENERATION_THREADS = 3

# The set of relations the model may emit (kept in sync with the enum in the prompt).
RELATIONS: frozenset[str] = frozenset(
    {"REMPLACE", "MODIFIE", "ABROGE", "RENVOIE", "DEFINIT"}
)

# The VALIDATED system prompt (copy of BRIEF-rag-ingestion-strategy.md § "Étape 2"). Do not
# reword casually: the directional fields + exact enum + one-shot example are what stabilised
# the label and the direction across runs.
REFERENCE_SYSTEM_PROMPT = (
    "Tu extrais les liens qu'UN article de contrat crée vers d'autres "
    "documents/articles/annexes. Renvoie UNIQUEMENT un tableau JSON. Chaque lien a "
    "EXACTEMENT ces 3 champs :\n"
    '  "relation" : un de EXACTEMENT '
    '["REMPLACE","MODIFIE","ABROGE","RENVOIE","DEFINIT"]\n'
    '  "ancien"   : l\'élément VISÉ/retiré (celui du contrat existant)\n'
    '  "nouveau"  : l\'élément qui le remplace, ou null si pas de remplacement\n'
    "Direction — le français « remplacer A par B » signifie : ancien=A, nouveau=B.\n"
    "Choix relation par le verbe : remplacer/substituer=>REMPLACE ; "
    "modifier/amender=>MODIFIE ; abroger/annuler=>ABROGE ; définir=>DEFINIT ; "
    "simple renvoi/voir=>RENVOIE.\n"
    "EXEMPLE — texte: « Les Parties conviennent de remplacer l'Annexe A du Contrat par "
    "l'Annexe B de l'Avenant. » => "
    '[{"relation":"REMPLACE","ancien":"Annexe A du Contrat","nouveau":"Annexe B de '
    "l'Avenant\"}]\n"
    "N'invente rien ; si aucun lien, renvoie []. Réponds UNIQUEMENT le JSON."
)


@dataclass
class Reference:
    """One reference edge extracted from a single article — the raw LLM output.

    `ancien` is the targeted/removed element (from the existing contract), `nouveau` the
    element that replaces it (None when the relation has no replacement, e.g. RENVOIE)."""

    relation: str
    ancien: str
    nouveau: str | None


@runtime_checkable
class Generator(Protocol):
    """The jettisonable generative slot: any chat backend that completes a (system, user)
    pair fits. One llama.cpp impl today; a hosted model swaps in behind the same seam."""

    name: str

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Return the assistant's message text for the given system + user prompts."""
        ...


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LlamaCppGenerator:
    """Chat generator backed by a GGUF model served by llama-server (llama.cpp).

    Disposable adapter (IT swaps binary / model / port via constructor args or env:
    GENERATION_BINARY / GENERATION_MODEL). The server is started lazily on the first
    complete() and stopped on close(); use it as a context manager so the process never
    leaks. Flags mirror the proven llama-swap entry for granite-4.0-h-tiny.
    """

    name = "llamacpp-granite"

    def __init__(
        self,
        *,
        binary_path: str | os.PathLike[str] | None = None,
        model_path: str | os.PathLike[str] | None = None,
        host: str = "127.0.0.1",
        port: int | None = None,
        threads: int = _GENERATION_THREADS,
        context_size: int = _GENERATION_CONTEXT_SIZE,
        max_output_tokens: int = 800,
        startup_timeout_seconds: float = 300.0,
        request_timeout_seconds: float = 420.0,
    ) -> None:
        self._binary_path = Path(
            binary_path
            or os.environ.get("GENERATION_BINARY", _DEFAULT_GENERATION_BINARY)
        )
        self._model_path = Path(
            model_path or os.environ.get("GENERATION_MODEL", _DEFAULT_GENERATION_MODEL)
        )
        self._host = host
        self._port = port
        self._threads = threads
        self._context_size = context_size
        self._max_output_tokens = max_output_tokens
        self._startup_timeout_seconds = startup_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> LlamaCppGenerator:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def _ensure_server(self) -> None:
        if self._process is not None:
            return
        if not self._binary_path.exists():
            raise RuntimeError(f"llama-server binary not found: {self._binary_path}")
        if not self._model_path.exists():
            raise RuntimeError(f"generation model not found: {self._model_path}")
        if self._port is None:
            self._port = _find_free_port()
        self._process = subprocess.Popen(
            [
                str(self._binary_path),
                "-m",
                str(self._model_path),
                "--host",
                self._host,
                "--port",
                str(self._port),
                "-c",
                str(self._context_size),
                "-t",
                str(self._threads),
                "-fa",
                "on",
                "-ngl",
                "0",
                "--mmap",
                "--jinja",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self._startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("llama-server exited before becoming ready")
            try:
                with urllib.request.urlopen(
                    f"{self._base_url()}/health", timeout=2
                ) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError):
                pass  # not up yet — the model is still loading on CPU
            time.sleep(0.5)
        self.close()
        raise RuntimeError("llama-server did not become ready in time")

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self._ensure_server()
        payload = json.dumps(
            {
                "model": "granite",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.0,
                # Bound generation time on CPU: a runaway completion on a long legal article
                # is what times a call out. A reference list rarely needs more than this.
                "max_tokens": self._max_output_tokens,
                "stream": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url()}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._request_timeout_seconds
            ) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(
                f"generation server HTTP {error.code}: {detail}"
            ) from error
        return body["choices"][0]["message"]["content"]

    def close(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None


# A reference NAMES an element ("Annexe 4 du Contrat", ~20 chars); anything much longer is
# the model summarising prose as a fake edge (observed on a table-heavy PRESTATIONS article
# with no real references — dozens of RENVOIE whose "ancien" was a whole paragraph). Drop
# those: it is both a quality filter and what keeps a runaway array from mattering.
_MAX_REFERENCE_LENGTH = 160


def _extract_reference_objects(raw: str) -> list[dict[str, object]]:
    """Salvage the top-level JSON objects from the FIRST array in the model's reply.

    The model is asked for a bare array but may wrap it in ``` fences, add prose, or — when
    it rambles into a long noisy list and hits the token cap — emit a TRUNCATED array. This
    scans object-by-object (string-aware) and returns every COMPLETE object, ignoring an
    unclosed trailing one. Fail-loud only when there is NO array at all (a refusal or broken
    prompt); a truncated array is an expected batch condition, not a bug to hide."""
    start = raw.find("[")
    if start == -1:
        raise ValueError(f"no JSON array in model reply: {raw[:200]!r}")
    objects: list[dict[str, object]] = []
    depth = 0
    in_string = False
    escaped = False
    object_start = -1
    for position in range(start + 1, len(raw)):
        character = raw[position]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                object_start = position
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0 and object_start != -1:
                try:
                    parsed = json.loads(raw[object_start : position + 1])
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    objects.append(parsed)
                object_start = -1
        elif character == "]" and depth == 0:
            break
    return objects


def parse_references(raw: str) -> list[Reference]:
    """Parse the model's reply into validated `Reference` edges (pure — no server).

    Keeps only well-formed edges: a `relation` in the known enum, a non-empty `ancien`, and
    endpoints short enough to be an element NAME rather than prose. A malformed or oversized
    individual edge is dropped; a truncated array is salvaged (see
    `_extract_reference_objects`); a reply with NO array at all is fail-loud."""
    references: list[Reference] = []
    for item in _extract_reference_objects(raw):
        relation = str(item.get("relation", "")).strip().upper()
        ancien = item.get("ancien")
        if (
            relation not in RELATIONS
            or not isinstance(ancien, str)
            or not ancien.strip()
        ):
            continue
        if len(ancien) > _MAX_REFERENCE_LENGTH:
            continue  # a paragraph, not a reference — the model summarising prose
        nouveau = item.get("nouveau")
        nouveau_text = (
            nouveau.strip() if isinstance(nouveau, str) and nouveau.strip() else None
        )
        if nouveau_text is not None and len(nouveau_text) > _MAX_REFERENCE_LENGTH:
            nouveau_text = None
        references.append(Reference(relation, ancien.strip(), nouveau_text))
    return references


# Cap the article text sent for extraction. `segment_articles` sizes chunks by CONTENT
# tokens (short tokens + stopwords dropped), which badly under-counts MODEL tokens on a
# number/table-dense article — one such chunk can be many pages of raw text, and its CPU
# prefill times the call out. A character cap is a direct proxy for model tokens (dense
# legal text runs ~1.75-3 chars/token, so 8000 chars stays well under the 16384 context).
# Reference clauses cluster at an article's HEAD, so truncating the tail is acceptable here;
# the proper cure is tokenizer-aware chunking (known debt).
_MAX_ARTICLE_CHARACTERS = 6000


def extract_references(
    generator: Generator,
    article_text: str,
    max_characters: int = _MAX_ARTICLE_CHARACTERS,
) -> list[Reference]:
    """Ask the generator for the reference edges an article creates, and parse them."""
    reply = generator.complete(REFERENCE_SYSTEM_PROMPT, article_text[:max_characters])
    return parse_references(reply)
