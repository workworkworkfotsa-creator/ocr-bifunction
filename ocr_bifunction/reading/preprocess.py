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


def _order_corners(points: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    coordinate_sum = points.sum(axis=1)
    coordinate_diff = np.diff(points, axis=1).ravel()
    return np.array(
        [
            points[np.argmin(coordinate_sum)],  # top-left  (smallest x+y)
            points[np.argmin(coordinate_diff)],  # top-right (smallest y-x)
            points[np.argmax(coordinate_sum)],  # bottom-right (largest x+y)
            points[np.argmax(coordinate_diff)],  # bottom-left (largest y-x)
        ],
        dtype="float32",
    )


def _distance(point_a: np.ndarray, point_b: np.ndarray) -> float:
    return float(np.hypot(point_a[0] - point_b[0], point_a[1] - point_b[1]))


class PerspectiveRectifier:
    """Detect the document quadrilateral and warp the trapezoid to a flat rectangle.

    The warp is trivial; the hard part — the one a bare OCR engine cannot do — is
    finding the 4 corners. We use the classic document-scanner heuristic (edges ->
    largest convex quad). When no convincing quad is found we return the image
    UNCHANGED: a wrong warp is worse than none.
    """

    name = "rectify"

    def __init__(self, minimum_area_ratio: float = 0.2) -> None:
        self.minimum_area_ratio = minimum_area_ratio

    def process(self, image_png_bytes: bytes) -> bytes:
        image = _decode(image_png_bytes)
        quadrilateral = self._find_document_quad(image)
        if quadrilateral is None:
            return image_png_bytes
        return _encode_png(self._warp(image, quadrilateral))

    def _find_document_quad(self, image: np.ndarray) -> np.ndarray | None:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(
            edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        image_area = image.shape[0] * image.shape[1]
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            perimeter = cv2.arcLength(contour, True)
            approximation = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
            if (
                len(approximation) == 4
                and cv2.contourArea(approximation)
                >= self.minimum_area_ratio * image_area
            ):
                return approximation.reshape(4, 2).astype("float32")
        return None

    def _warp(self, image: np.ndarray, quadrilateral: np.ndarray) -> np.ndarray:
        top_left, top_right, bottom_right, bottom_left = _order_corners(quadrilateral)
        width = int(
            max(_distance(bottom_right, bottom_left), _distance(top_right, top_left))
        )
        height = int(
            max(_distance(top_right, bottom_right), _distance(top_left, bottom_left))
        )
        destination = np.array(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
            dtype="float32",
        )
        matrix = cv2.getPerspectiveTransform(_order_corners(quadrilateral), destination)
        return cv2.warpPerspective(image, matrix, (width, height))
