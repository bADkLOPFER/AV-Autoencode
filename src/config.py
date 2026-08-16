from __future__ import annotations

import json
import logging
import sys
import shutil
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

DEFAULT_ENCODER = "nvencc64" if IS_WINDOWS else "ffmpeg"
ENCODER_CHOICES = ("ffmpeg", "nvencc", "nvencc64")
DEFAULT_WEB_PORT = 8265

# VMAF-Standardwert abhängig vom Betriebssystem
DEFAULT_VMAF_BIN = "vmaf.exe" if IS_WINDOWS else "vmaf"

# 2. Standard-Konfiguration definieren
def get_default_config() -> Dict[str, Any]:
    return {
        "base_dir": "./",
        "tools": {
            "ffmpeg": "ffmpeg",
            "ffprobe": "ffprobe",
            # Auf Linux/macOS 'nvencc' oder weglassen, auf Windows 'nvencc64' bzw. 'nvencc'
            "nvencc": "nvencc64" if IS_WINDOWS else "nvencc",
            "vmaf": "vmaf.exe" if IS_WINDOWS else "vmaf",
        },
        "default_encoder": DEFAULT_ENCODER, # dynamisch: 'nvencc64' (Win) vs 'ffmpeg' (Linux)
        "default_codec": "av1",
        "default_ai_choice": "2",
    }

def verify_tools(config: Dict[str, Any]) -> bool:
    """
    Überprüft, ob alle in der config.json konfigurierten Tools
    auf dem aktuellen Betriebssystem existieren und lauffähig sind.
    """
    tools = config.get("tools", {})
    all_valid = True

    logger.info("Starte Überprüfung der Encoding-Tools...")

    for tool_name, tool_path in tools.items():
        # 1. Prüfen, ob es ein expliziter Pfad (absolut oder mit Slashes) ist
        if Path(tool_path).is_absolute() or "\\" in tool_path or "/" in tool_path:
            if not Path(tool_path).exists():
                logger.error(f"❌ Tool '{tool_name}' nicht gefunden unter Pfad: {tool_path}")
                all_valid = False
            else:
                logger.info(f"✓ Tool '{tool_name}' gefunden: {tool_path}")
        else:
            # 2. Ansonsten im System-PATH suchen (z.B. für globale Installationen oder 'vmaf.exe')
            resolved_path = shutil.which(tool_path)
            if not resolved_path:
                logger.error(f"❌ Tool '{tool_name}' ('{tool_path}') weder als Pfad noch im System-PATH gefunden!")
                all_valid = False
            else:
                logger.info(f"✓ Tool '{tool_name}' im System gefunden: {resolved_path}")

    return all_valid

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
        default_cfg = get_default_config()
        save_config(default_cfg, config_file)
        config = default_cfg()
    else:
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            logger.error(f"Fehler beim Laden von {config_file}: {e}. Verwende Standardwerte.")
            config = default_cfg.copy()

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

if not verify_tools(CONFIG):
    logger.critical("Kritischer Fehler: Mindestens ein Encoding-Tool fehlt oder ist falsch konfiguriert!")
    # Optional: Den Start hier hart abbrechen, damit keine fehlerhaften Jobs starten
    sys.exit(1)

def resolve_encoder_choice(requested: Optional[str]) -> str:
    if requested is None:
        return DEFAULT_ENCODER

    normalized = requested.lower()
    if normalized not in ENCODER_CHOICES:
        raise ValueError(f"Unsupported encoder '{requested}'. Expected one of {ENCODER_CHOICES}.")

    if normalized == "nvencc" and not IS_WINDOWS:
        raise ValueError("NVEncC is only supported on Windows. Please use ffmpeg on macOS or Linux.")

    return normalized