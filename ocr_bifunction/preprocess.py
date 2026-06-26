"""Pre-OCR image enhancement — a universal, category-agnostic slot in the OCR lane.

Acts on pixels, BEFORE recognition (and before ② / templates). One shared module,
not per-template — that is the sense in which it is "universal". But universal
availability is not a universal *chain*: clean images (recto, screenshots) already
read at 0.97+ raw, and aggressive thresholding regresses them. So the default is a
no-op, and the enhancement chain is meant to be armed only on the hard cases (the
ID-card verso, whose wavy guilloché background drowns the MRZ).

The chain mirrors the user's field-proven Node.js/Tesseract recipe: desaturate
(grayscale) + blur (median, kills the small waves) + a filter (adaptive threshold).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import cv2
import numpy as np


@runtime_checkable
class Preprocessor(Protocol):
    """The jettisonable pre-OCR slot. PNG bytes in, PNG bytes out."""

    name: str

    def process(self, image_png_bytes: bytes) -> bytes: ...


def _decode(image_png_bytes: bytes) -> np.ndarray:
    return cv2.imdecode(
        np.frombuffer(image_png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
    )


def _encode_png(image: np.ndarray) -> bytes:
    return cv2.imencode(".png", image)[1].tobytes()


class NoPreprocessor:
    """Identity — the safe default that never regresses a clean image."""

    name = "raw"

    def process(self, image_png_bytes: bytes) -> bytes:
        return image_png_bytes


class EnhancePreprocessor:
    """Desaturate + median blur + adaptive threshold — for noisy/guilloché scans.

    Parameters are tunable knobs, not constants of nature: the A/B on real hard
    cases (the verso) decides their values. upscale helps small MRZ-sized text.
    """

    name = "enhance"

    def __init__(
        self,
        upscale: float = 2.0,
        median_blur_kernel: int = 3,
        threshold_block_size: int = 35,
        threshold_constant: float = 11.0,
    ) -> None:
        self.upscale = upscale
        self.median_blur_kernel = median_blur_kernel
        self.threshold_block_size = threshold_block_size
        self.threshold_constant = threshold_constant

    def process(self, image_png_bytes: bytes) -> bytes:
        image = _decode(image_png_bytes)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if self.upscale != 1.0:
            gray = cv2.resize(
                gray,
                None,
                fx=self.upscale,
                fy=self.upscale,
                interpolation=cv2.INTER_CUBIC,
            )
        denoised = cv2.medianBlur(gray, self.median_blur_kernel)
        binarized = cv2.adaptiveThreshold(
            denoised,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            self.threshold_block_size,
            self.threshold_constant,
        )
        # Back to 3-channel so any OcrEngine consumes it like a normal image.
        return _encode_png(cv2.cvtColor(binarized, cv2.COLOR_GRAY2BGR))
