"""LightOnOCR-2 — the VLM escalation OcrEngine for the batch / escalade lane.

A 1B vision-language OCR model (LightOn, French/EU — preferred for PII per RGPD), served by
the shared llama-swap proxy as a multimodal llama-server (GGUF + its vision projector mmproj),
CPU-only. It is the HEAVY fallback for the hard cases RapidOCR cannot read (e.g. an ID-card MRZ
that fails to parse). Per the cadrage it is **batch / escalade only, NEVER the API fast-path**
(~171 s/img, ~1.8 GB RAM on the 8 GB target). It plugs behind the same jettisonable OcrEngine
slot as RapidOCR and Docling.

Like the generator and the embedding retriever, it is a thin CLIENT of llama-swap: it owns no
process (llama-swap lazy-loads the model by key and TTL-unloads it). The image is sent to the
OpenAI-compatible /v1/chat/completions endpoint as a base64 data URL; llama-swap must serve the
`lightonocr-2-1b` key with a multimodal llama-server (mmproj in its config). Override the
endpoint / model via env (LLAMA_SWAP_URL / LIGHTONOCR_MODEL_KEY).

The model emits markdown text, not boxes, so the TextLines carry NO real geometry (a synthetic
top-to-bottom bbox preserves reading order only). This engine is therefore for CONTENT-based
extraction (the MRZ, read by character pattern), not the geometry-anchored recto templates.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

from ocr_bifunction.reader import TextLine

_DEFAULT_LLAMA_SWAP_URL = "http://127.0.0.1:8080"
_DEFAULT_MODEL_KEY = "lightonocr-2-1b"

DEFAULT_PROMPT = (
    "Transcribe all the text in this image, including the machine-readable zone "
    "(MRZ) lines at the bottom. Output the text exactly as printed."
)


def _lines_from_text(transcription: str) -> list[TextLine]:
    """Turn the model's markdown transcription into ordered TextLines.

    No real geometry is available (the VLM returns text, not boxes), so each line gets a
    synthetic top-to-bottom bbox that preserves reading order — enough for content-based
    extraction (MRZ). Markdown image placeholders (``![...](...)``) are dropped.
    """
    lines: list[TextLine] = []
    y_cursor = 0.0
    for raw_line in transcription.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("!["):
            continue
        lines.append(
            TextLine(
                text=stripped,
                bbox=(0.0, y_cursor, 1000.0, y_cursor + 10.0),
                confidence=None,  # a VLM exposes no per-line OCR score
            )
        )
        y_cursor += 12.0
    return lines


class LightOnOcrEngine:
    name = "lightonocr-2-1b"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model_key: str | None = None,
        prompt: str = DEFAULT_PROMPT,
        max_tokens: int = 2048,
        request_timeout_seconds: float = 600.0,
    ) -> None:
        self._base_url = (
            base_url or os.environ.get("LLAMA_SWAP_URL", _DEFAULT_LLAMA_SWAP_URL)
        ).rstrip("/")
        self._model_key = model_key or os.environ.get(
            "LIGHTONOCR_MODEL_KEY", _DEFAULT_MODEL_KEY
        )
        self._prompt = prompt
        self._max_tokens = max_tokens
        self._request_timeout_seconds = request_timeout_seconds

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        return _lines_from_text(self._transcribe(image_png_bytes))

    def _transcribe(self, image_bytes: bytes) -> str:
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode(
            "ascii"
        )
        payload = json.dumps(
            {
                "model": self._model_key,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self._prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "temperature": 0.0,
                "max_tokens": self._max_tokens,
                "stream": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
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
                f"lightonocr server HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                f"shared llama-swap unreachable at {self._base_url} "
                f"(is it running?): {error.reason}"
            ) from error
        return body["choices"][0]["message"]["content"]
