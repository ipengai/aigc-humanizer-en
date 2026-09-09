"""Public name for the local Turnitin-fit detector adapter.

The factory in :mod:`app.ai_detector.adapter` owns lazy loading so the web app
can still start in legacy Sapling mode when scientific-model dependencies are
not installed. This module documents the adapter boundary and is intentionally
small; callers should use ``create_detector('turnitin_fit_detector_v2')``.
"""

BACKEND = "turnitin_fit_detector_v2"
MODEL_VERSION = "v2.1.0"
