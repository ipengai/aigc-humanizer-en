"""Detector + humanizer routing and execution pipeline."""

from app.pipeline.router import (
    LEGACY_POLICY,
    SEGMENTED_POLICY,
    choose_document_policy,
    choose_rewrite_route,
)

__all__ = [
    "LEGACY_POLICY",
    "SEGMENTED_POLICY",
    "choose_document_policy",
    "choose_rewrite_route",
]
