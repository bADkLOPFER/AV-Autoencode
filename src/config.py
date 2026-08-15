from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .utils import _clamp
    from .paths import PATHS
except ImportError: 
    from utils import _clamp
    from paths import PATHS


SCRIPT_DIR = Path(__file__).resolve().parent

def detect_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


PROJECT_ROOT = SCRIPT_DIR.parent

PLATFORM = detect_platform()
IS_WINDOWS = PLATFORM == "windows"
IS_MACOS = PLATFORM == "macos"
IS_LINUX = PLATFORM == "linux"


DEFAULT_ENCODER = "nvencc" if IS_WINDOWS else "ffmpeg"
ENCODER_CHOICES = ("ffmpeg", "nvencc")

DEFAULT_WEB_PORT = 8265

def resolve_encoder_choice(requested: Optional[str]) -> str:
    if requested is None:
        return DEFAULT_ENCODER

    normalized = requested.lower()
    if normalized not in ENCODER_CHOICES:
        raise ValueError(f"Unsupported encoder '{requested}'. Expected one of {ENCODER_CHOICES}.")

    if normalized == "nvencc" and not IS_WINDOWS:
        raise ValueError("NVEncC is only supported on Windows. Please use ffmpeg on macOS or Linux.")

    return normalized


CONFIG = {
    "platform": PLATFORM,
    "encoder": DEFAULT_ENCODER,
    "paths": PATHS,
    "debug_args": True,
}
WORKFLOW_DEFAULTS = {
    "hw_accel_mode": "auto",          # "auto", "nvencc", oder "cpu"
    "default_codec": "av1",           # "av1" oder "hevc"
    "default_true_hdr": True,         # TrueHDR standardmäßig aktivieren
    "disk_space_multiplier": 1.5,     # Sicherheitsfaktor für freien Speicherplatz
}