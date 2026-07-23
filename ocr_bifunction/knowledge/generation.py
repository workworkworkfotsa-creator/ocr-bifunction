"""Generative slot — the LLM tier that extracts reference EDGES from contract articles.

Jettisonable adapter that is a thin CLIENT of the shared **llama-swap** proxy — it does NOT
spawn or own a llama-server. This machine is shared by several SLM projects (cf. memory
`shared-machine-3-slm-projects`) and the real pain is FORGETTING to start/stop a service, not
RAM (one model runs at a time, serialised by hand). llama-swap fixes exactly that: it
lazy-LOADS the model by key on first request (nothing to remember to start) and TTL-UNLOADS it
when idle (nothing to remember to stop). So this generator just POSTs to llama-swap and never
manages a process — `close()` is a no-op; there is no server to leak or to `taskkill`.

The one job today is REFERENCE EXTRACTION. Given one article's text, the model returns the
links it creates to other documents/articles/annexes as DIRECTIONAL edges (ancien/nouveau).
The prompt is the one PROVEN on the real Avenant Article 2 (2026-07-01): a free `cible/par`
schema was unstable (both the label and the direction flipped between runs), so it is pinned
by directional fields + an exact enum + a one-shot example. The HTTP transport is generic; the
prompt and the JSON parsing are pure functions on top, so parsing is testable without a server.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ocr_bifunction.llama_transport import post_json, resolve_base_url

# The model KEY is the entry in llama-swap's config.yaml; the shared base URL + POST transport
# live in llama_transport. IT / other projects override the model via env (GENERATION_MODEL_KEY).
_DEFAULT_GENERATION_MODEL_KEY = "granite-4.0-h-tiny-Q4_K_M"

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


class LlamaSwapGenerator:
    """Chat generator that is a thin CLIENT of the shared llama-swap proxy.

    It owns NO process: llama-swap lazy-loads the model by `model_key` on the first request
    (nothing to start) and TTL-unloads it when idle (nothing to stop). Override the endpoint /
    model / limits via constructor args or env (LLAMA_SWAP_URL, GENERATION_MODEL_KEY). `close()`
    is a no-op and the context-manager methods exist only so callers can keep using `with`.
    The first call may block while llama-swap loads the model on CPU, so the request timeout is
    generous.
    """

    name = "llama-swap-granite"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model_key: str | None = None,
        max_output_tokens: int = 800,
        request_timeout_seconds: float = 420.0,
    ) -> None:
        self._base_url = resolve_base_url(base_url)
        self._model_key = model_key or os.environ.get(
            "GENERATION_MODEL_KEY", _DEFAULT_GENERATION_MODEL_KEY
        )
        self._max_output_tokens = max_output_tokens
        self._request_timeout_seconds = request_timeout_seconds

    def __enter__(self) -> LlamaSwapGenerator:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        body = post_json(
            self._base_url,
            "/v1/chat/completions",
            {
                "model": self._model_key,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.0,
                # Bound generation time on CPU: a runaway completion on a long legal article
                # is what times a call out. A reference list rarely needs more than this.
                "max_tokens": self._max_output_tokens,
                "stream": False,
            },
            timeout=self._request_timeout_seconds,
            server_label="generation",
        )
        return body["choices"][0]["message"]["content"]

    def close(self) -> None:
        """No-op: llama-swap owns the model lifecycle (TTL-unload); nothing to stop here."""


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
