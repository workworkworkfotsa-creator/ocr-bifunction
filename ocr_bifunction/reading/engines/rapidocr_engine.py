"""RapidOCR — the first concrete OcrEngine behind the jettisonable slot.

RapidOCR is a classic detection + recognition OCR (ONNX, CPU-only, no GPU). It
returns, per line, the recognized text, a quadrilateral box and a confidence
score. We keep the box (reduced to an axis-aligned bbox): it is the spatial anchor
stage ③ rebuilds fields from. The score is the legibility signal the confidence
gate routes on (low score = "douteux → humain").
"""

from __future__ import annotations

import cv2
import numpy as np
from rapidocr import RapidOCR

from ocr_bifunction.reading.reader import TextLine


class RapidOcrEngine:
    name = "rapidocr"

    def __init__(self) -> None:
        # First construction downloads the ONNX detection/recognition models to cache.
        self._engine = RapidOCR()

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        image_bgr = cv2.imdecode(
            np.frombuffer(image_png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        result = self._engine(image_bgr)
        if result.txts is None:
            return []
        # Boxes come back in PIXELS of this image, so the image IS the page frame of
        # reference — carried on every line so provenance can be normalized downstream.
        image_height, image_width = image_bgr.shape[:2]
        lines: list[TextLine] = []
        for text, quadrilateral, score in zip(result.txts, result.boxes, result.scores):
            xs = [float(point[0]) for point in quadrilateral]
            ys = [float(point[1]) for point in quadrilateral]
            lines.append(
                TextLine(
                    text=text,
                    bbox=(min(xs), min(ys), max(xs), max(ys)),
                    confidence=float(score),
                    page_width=float(image_width),
                    page_height=float(image_height),
                )
            )
        return lines
