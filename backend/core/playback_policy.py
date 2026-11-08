from __future__ import annotations

import os


def configured_completion_threshold() -> float:
    try:
        value = float(os.getenv("PLAYBACK_COMPLETION_THRESHOLD", "0.95"))
    except (TypeError, ValueError):
        value = 0.95
    return min(1.0, max(0.5, value))


COMPLETION_THRESHOLD = configured_completion_threshold()
