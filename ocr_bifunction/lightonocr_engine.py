"""LightOnOCR-2 — the VLM escalation OcrEngine for the batch / escalade lane.

A 1B vision-language OCR model (LightOn, French/EU — preferred for PII per RGPD) run
through llama.cpp's multimodal CLI (`llama-mtmd-cli`), CPU-only. It is the HEAVY fallback
for the hard cases RapidOCR cannot read (e.g. an ID-card MRZ that fails to parse): proven
to recover a TD1 MRZ that RapidOCR misses, all check digits passing. Per the cadrage it is
**batch / escalade only, NEVER the API fast-path** (~171 s/img, ~1.8 GB RAM on the 8 GB
target). It plugs behind the same jettisonable OcrEngine slot as RapidOCR and Docling.

It shells out to the llama.cpp binary instead of binding a Python lib: the proven runtime
is a prebuilt llama.cpp (build b9542) with the LightOnOCR-2 GGUF + its vision projector
(mmproj). IT swaps the binary / model / mmproj via constructor args or env vars
(LIGHTONOCR_BINARY / LIGHTONOCR_MODEL / LIGHTONOCR_MMPROJ) — the disposable adapter.

The model emits markdown text, not boxes, so the TextLines carry NO real geometry (a
synthetic top-to-bottom bbox preserves reading order only). This engine is therefore for
CONTENT-based extraction (the MRZ, read by character pattern), not the geometry-anchored
recto templates.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from ocr_bifunction.reader import TextLine

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MODEL = _REPO_ROOT / "models" / "LightOnOCR-2-1B-Q8_0.gguf"
_DEFAULT_MMPROJ = _REPO_ROOT / "models" / "mmproj-LightOnOCR-2-1B-Q8_0.gguf"
# The llama.cpp multimodal CLI this POC was proven on. IT overrides via LIGHTONOCR_BINARY.
_DEFAULT_BINARY = r"C:\Users\filipeparente\Tools\llamacpp\b9542\llama-mtmd-cli.exe"

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
        binary_path: str | os.PathLike[str] | None = None,
        model_path: str | os.PathLike[str] | None = None,
        mmproj_path: str | os.PathLike[str] | None = None,
        threads: int = 4,
        context_size: int = 4096,
        max_tokens: int = 1024,
        prompt: str = DEFAULT_PROMPT,
        timeout_seconds: float = 600.0,
    ) -> None:
        self._binary_path = Path(
            binary_path or os.environ.get("LIGHTONOCR_BINARY", _DEFAULT_BINARY)
        )
        self._model_path = Path(
            model_path or os.environ.get("LIGHTONOCR_MODEL", str(_DEFAULT_MODEL))
        )
        self._mmproj_path = Path(
            mmproj_path or os.environ.get("LIGHTONOCR_MMPROJ", str(_DEFAULT_MMPROJ))
        )
        self._threads = threads
        self._context_size = context_size
        self._max_tokens = max_tokens
        self._prompt = prompt
        self._timeout_seconds = timeout_seconds

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        return _lines_from_text(self._transcribe(image_png_bytes))

    def _transcribe(self, image_bytes: bytes) -> str:
        # The CLI treats commas in --image as a path separator and mangles non-ASCII argv
        # on Windows; writing to an ASCII temp path sidesteps both. stb decodes by content,
        # so the .png suffix is cosmetic.
        with tempfile.TemporaryDirectory(prefix="lightonocr_") as temp_directory:
            image_path = Path(temp_directory) / "page.png"
            image_path.write_bytes(image_bytes)
            completed = subprocess.run(
                [
                    str(self._binary_path),
                    "-m",
                    str(self._model_path),
                    "--mmproj",
                    str(self._mmproj_path),
                    "--image",
                    str(image_path),
                    "-p",
                    self._prompt,
                    "-ngl",
                    "0",
                    "-t",
                    str(self._threads),
                    "--temp",
                    "0",
                    "-c",
                    str(self._context_size),
                    "-n",
                    str(self._max_tokens),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
            )
        if completed.returncode != 0:
            raise RuntimeError(
                f"llama-mtmd-cli failed (exit {completed.returncode}): "
                f"{completed.stderr[-500:]}"
            )
        return completed.stdout
