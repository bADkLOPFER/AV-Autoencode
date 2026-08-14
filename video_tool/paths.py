from __future__ import annotations

import sys
from pathlib import Path

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
        "ffmpeg_vmaf": Path("/usr/local/bin/ffmpeg"),
        "ffprobe": Path("/usr/bin/ffprobe"),
        "nvencc": Path("/usr/local/bin/nvencc"),
        "results": SCRIPT_DIR / "Results",
    }
