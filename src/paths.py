from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

def detect_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"

PLATFORM = detect_platform()
IS_WINDOWS = PLATFORM == "windows"
IS_MACOS = PLATFORM == "macos"
IS_LINUX = PLATFORM == "linux"

# --- Headless Ordnerstruktur ---
INBOX_DIR = PROJECT_ROOT / "Inbox"
WORK_DIR = PROJECT_ROOT / "Work"
RESULT_DIR = PROJECT_ROOT / "Result"
DONE_DIR = PROJECT_ROOT / "Done"
CONFIG_DIR = PROJECT_ROOT / "config"

def get_vmaf_path() -> Path:
    if IS_WINDOWS:
        # Pfad zu deiner vmaf.exe auf Windows
        return PROJECT_ROOT / "VMAF" / "vmaf.exe"
    else:
        # Auf macOS / Linux liegt 'vmaf' meist im System-PATH (z. B. /opt/homebrew/bin/vmaf)
        return Path("vmaf")

def init_directories() -> None:
    """Erstellt alle benötigten Headless-Verzeichnisse."""
    for directory in [INBOX_DIR, WORK_DIR, RESULT_DIR, DONE_DIR, CONFIG_DIR]:
        directory.mkdir(parents=True, exist_ok=True)

if IS_WINDOWS:
    PATHS = {
        "ffmpeg": PROJECT_ROOT / "FFMPeg" / "ffmpeg.exe",
        "ffprobe": PROJECT_ROOT / "FFMPeg" / "ffprobe.exe",
        "nvencc": PROJECT_ROOT / "NVEncC" / "nvencc64.exe",
        "results": RESULT_DIR,
    }
elif IS_MACOS:
    PATHS = {
        "ffmpeg": Path("/opt/homebrew/bin/ffmpeg"),
        "ffprobe": Path("/opt/homebrew/bin/ffprobe"),
        "nvencc": None,
        "results": RESULT_DIR,
    }
else:
    PATHS = {
        "ffmpeg": Path("/usr/bin/ffmpeg"),
        "ffmpeg_vmaf": Path("/usr/local/bin/ffmpeg"),
        "ffprobe": Path("/usr/bin/ffprobe"),
        "nvencc": Path("/usr/local/bin/nvencc"),
        "results": RESULT_DIR,
    }

PATHS["vmaf"] = get_vmaf_path()

init_directories()
