from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional


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


if IS_WINDOWS:
    PATHS = {
        "ffmpeg": PROJECT_ROOT / "FFMPeg" / "ffmpeg.exe",
        "ffprobe": PROJECT_ROOT / "FFMPeg" / "ffprobe.exe",
        "nvencc": PROJECT_ROOT / "NVEncC" / "nvencc64.exe",
        "results": PROJECT_ROOT / "Results",
    }
elif IS_MACOS:
    PATHS = {
        "ffmpeg": Path("/opt/homebrew/bin/ffmpeg"),
        "ffprobe": Path("/opt/homebrew/bin/ffprobe"),
        "nvencc": None,
        "results": SCRIPT_DIR / "Results",
    }
else:
    PATHS = {
        "ffmpeg": Path("/usr/bin/ffmpeg"),
        "ffprobe": Path("/usr/bin/ffprobe"),
        "nvencc": Path("/usr/local/bin/nvencc"),
        "results": SCRIPT_DIR / "Results",
    }


DEFAULT_ENCODER = "nvencc" if IS_WINDOWS else "ffmpeg"
ENCODER_CHOICES = ("ffmpeg", "nvencc")


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
