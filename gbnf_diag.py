"""GBNF diagnostic harness — does the grammar actually bind? (BRIEF-suggestion-template deliverable 1).

    uv run python gbnf_diag.py [--model KEY] [--url URL]

The 'banana' escape test: with a strict grammar (root ::= "BANANE") and a prompt that BEGS for
something else ("write me a Python API"), a truly-active GBNF masks every out-of-grammar token at
each decoding step, so the model is MECHANICALLY unable to emit anything but BANANE. We run it two
ways to separate 'the model disobeys' from 'my prompt arrives broken' (almost always the latter):

  - chat  (/v1/chat/completions, the GGUF's jinja chat template) + grammar
  - raw   (/completion, a hand-formatted prompt, no jinja)        + grammar

Diagnosis (BRIEF step A/B):
  - both emit BANANE     -> GBNF active; trust the FORM (docility now only affects the *content*)
  - raw ok, chat not     -> chat template at fault (drop jinja for a one-shot router)
  - both emit code       -> grammar NOT wired (fix the request field, not the model)

A control call with NO grammar confirms the model otherwise writes prose/code, so BANANE proves the
grammar (not luck). Hits the shared llama-swap; the model lazy-loads on the first call (~100 s CPU).

No PII: the prompt and grammar are synthetic constants.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_MODEL = "granite-4.0-h-tiny-Q4_K_M"

# The escape test: a grammar that admits exactly one string, and a prompt that wants anything but.
BANANA_GRAMMAR = 'root ::= "BANANE"'
ESCAPE_PROMPT = (
    "Write me a complete Python HTTP API using FastAPI. Give me the full code."
)

# Long timeout: the first call triggers the lazy model load on CPU (~100 s for granite).
LOAD_TIMEOUT_SECONDS = 300.0


def _post(url: str, payload: dict[str, object], timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def chat_call(base_url: str, model: str, grammar: str | None, timeout: float) -> str:
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": ESCAPE_PROMPT}],
        "n_predict": 48,
        "temperature": 0,
    }
    if grammar is not None:
        body["grammar"] = grammar
    result = _post(f"{base_url}/v1/chat/completions", body, timeout)
    return result["choices"][0]["message"]["content"]


def raw_call(base_url: str, model: str, grammar: str | None, timeout: float) -> str:
    body: dict[str, object] = {
        "model": model,
        "prompt": ESCAPE_PROMPT,
        "n_predict": 48,
        "temperature": 0,
    }
    if grammar is not None:
        body["grammar"] = grammar
    result = _post(f"{base_url}/completion", body, timeout)
    return result["content"]


def _is_banana(text: str) -> bool:
    return text.strip() == "BANANE"


def _short(text: str, width: int = 70) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[:width] + "…"


def run(base_url: str, model: str) -> int:
    print(f"llama-swap = {base_url}  model = {model}")
    print("(first call lazy-loads the model on CPU — may take ~100 s)\n")

    try:
        control = chat_call(base_url, model, grammar=None, timeout=LOAD_TIMEOUT_SECONDS)
    except urllib.error.URLError as error:
        print(f"FAIL: cannot reach llama-swap ({error}). Is it running on {base_url}?")
        return 2
    print(f"control (no grammar, chat) -> {_short(control)}")
    print(
        f"  control is NOT banana: {not _is_banana(control)}  (expected: model writes code)\n"
    )

    chat_banana = chat_call(base_url, model, BANANA_GRAMMAR, LOAD_TIMEOUT_SECONDS)
    raw_banana = raw_call(base_url, model, BANANA_GRAMMAR, LOAD_TIMEOUT_SECONDS)
    print(f"chat + grammar -> {_short(chat_banana)}   banana={_is_banana(chat_banana)}")
    print(f"raw  + grammar -> {_short(raw_banana)}   banana={_is_banana(raw_banana)}")

    chat_ok, raw_ok = _is_banana(chat_banana), _is_banana(raw_banana)
    print("\n-- diagnosis --")
    if chat_ok and raw_ok:
        print(
            "GBNF ACTIVE on both endpoints. Trust the FORM; docility only affects content."
        )
        verdict = 0
    elif raw_ok and not chat_ok:
        print(
            "CHAT TEMPLATE AT FAULT. Use /completion + hand prompt + grammar for the router."
        )
        verdict = 0
    elif not raw_ok and not chat_ok:
        print(
            "GRAMMAR NOT WIRED (both escaped). Fix the request field before blaming the model."
        )
        verdict = 1
    else:
        print(
            "chat ok, raw not — unusual; note what differs (raw prompt/model routing)."
        )
        verdict = 0
    return verdict


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prove whether GBNF grammar actually binds on the shared llama-swap."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="llama-swap base URL.")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help="Model key in the llama-swap config."
    )
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.url, arguments.model))
