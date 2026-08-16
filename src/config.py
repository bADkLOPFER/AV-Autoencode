from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Einheitlicher Logger-Name für das gesamte Projekt
logger = logging.getLogger("omni_pipeline")

# Pfad zur Konfigurationsdatei im Root-Verzeichnis
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent


# 1. Plattform-Erkennung vorab durchführen
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

DEFAULT_ENCODER = "nvencc" if IS_WINDOWS else "ffmpeg"
ENCODER_CHOICES = ("ffmpeg", "nvencc")
DEFAULT_WEB_PORT = 8265

# VMAF-Standardwert abhängig vom Betriebssystem
DEFAULT_VMAF_BIN = "vmaf.exe" if IS_WINDOWS else "vmaf"

# 2. Standard-Konfiguration definieren
DEFAULT_CONFIG: Dict[str, Any] = {
    "base_dir": "./",
    "tools": {
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "nvencc": "nvencc",
        "vmaf": DEFAULT_VMAF_BIN,
    },
    "default_codec": "av1",
    "default_ai_choice": "2",
}


# 3. Funktionen zum Laden und Speichern
def save_config(config_data: Dict[str, Any], config_file: Path = CONFIG_PATH) -> None:
    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Konfiguration: {e}")


def load_config(config_file: Path = CONFIG_PATH) -> Dict[str, Any]:
    if not config_file.exists():
        logger.info(f"Keine config.json gefunden. Erstelle Standard-Konfiguration unter {config_file}")
        save_config(DEFAULT_CONFIG, config_file)
        config = DEFAULT_CONFIG.copy()
    else:
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            logger.error(f"Fehler beim Laden von {config_file}: {e}. Verwende Standardwerte.")
            config = DEFAULT_CONFIG.copy()

    # Automatische Ableitung der Ordnerstrukturen aus base_dir
    base_path = Path(config.get("base_dir", "./")).resolve()
    config["inbox_dir"] = str(base_path / "Inbox")
    config["work_dir"] = str(base_path / "Work")
    config["result_dir"] = str(base_path / "Result")
    config["done_dir"] = str(base_path / "Done")

    # Sicherstellen, dass Ordner existieren
    for key in ["inbox_dir", "work_dir", "result_dir", "done_dir"]:
        Path(config[key]).mkdir(parents=True, exist_ok=True)

    return config


# 4. Globales Config-Objekt NACH allen Definitionen initialisieren
CONFIG = load_config()


def resolve_encoder_choice(requested: Optional[str]) -> str:
    if requested is None:
        return DEFAULT_ENCODER

    normalized = requested.lower()
    if normalized not in ENCODER_CHOICES:
        raise ValueError(f"Unsupported encoder '{requested}'. Expected one of {ENCODER_CHOICES}.")

    if normalized == "nvencc" and not IS_WINDOWS:
        raise ValueError("NVEncC is only supported on Windows. Please use ffmpeg on macOS or Linux.")

    return normalized