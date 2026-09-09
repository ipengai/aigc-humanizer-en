"""Process-local runtime for the vendored turnitin-fit random forest."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import joblib

from app.ai_detector.vendor.turnitin_fit_detector_v2 import random_forest_detector


DEFAULT_METADATA_PATH = (
    Path(__file__).resolve().parent
    / "vendor"
    / "turnitin_fit_detector_v2"
    / "random_forest_detector.json"
)


class TurnitinFitDetectorRuntime:
    """Load metadata and classifier once per worker process."""

    def __init__(self, metadata_path=DEFAULT_METADATA_PATH):
        self.metadata_path = Path(metadata_path)
        self._lock = threading.Lock()
        self._metadata = None
        self._classifier = None
        self.load_count = 0

    def ensure_loaded(self):
        if self._classifier is not None:
            return self._metadata, self._classifier
        with self._lock:
            if self._classifier is None:
                metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
                if metadata.get("feature_names") != list(random_forest_detector.FEATURE_NAMES):
                    raise ValueError("模型与检测器特征版本不一致")
                model_path = self.metadata_path.with_name(metadata["model_file"])
                self._classifier = joblib.load(model_path)
                self._metadata = metadata
                self.load_count += 1
        return self._metadata, self._classifier

    @property
    def metadata(self):
        return self.ensure_loaded()[0]

    @property
    def classifier(self):
        return self.ensure_loaded()[1]

    def detect(self, text):
        metadata, classifier = self.ensure_loaded()
        return random_forest_detector.detect(
            text,
            self.metadata_path,
            metadata=metadata,
            classifier=classifier,
        )

    def warm_up(self):
        self.ensure_loaded()
