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
PLATFORM_NAME = PLATFORM.upper()

IS_WINDOWS = PLATFORM == "windows"
IS_MACOS = PLATFORM == "macos"
IS_LINUX = PLATFORM == "linux"

DEFAULT_ENCODER = "nvencc64" if IS_WINDOWS else "ffmpeg"
ENCODER_CHOICES = ("ffmpeg", "nvencc", "nvencc64", "qsv", "vcenc", "vceenc")
DEFAULT_WEB_PORT = 8265
DEFAULT_VMAF_BIN = "vmaf.exe" if IS_WINDOWS else "vmaf"


# 2. Hilfsfunktionen zur Konfiguration
def _ensure_exe(path_str: str) -> str:
    """Normalisiert plattformspezifische Tool-Endungen."""
    path_str = str(path_str)
    if IS_WINDOWS and not path_str.lower().endswith(".exe"):
        return f"{path_str}.exe"
    if not IS_WINDOWS and path_str.lower().endswith(".exe"):
        return path_str[:-4]
    return path_str


def resolve_encoder_choice(requested: Optional[str]) -> str:
    """Ermittelt den zu nutzenden Encoder mit Fallback."""
    if requested is None:
        return DEFAULT_ENCODER

    normalized = requested.lower().strip()
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    if normalized == "vceenc":
        normalized = "vcenc"
    if normalized not in ENCODER_CHOICES:
        logger.warning(
            f"Ungültiger Encoder '{requested}'. Erlaubt sind: {ENCODER_CHOICES}. Fallback auf '{DEFAULT_ENCODER}'."
        )
        return DEFAULT_ENCODER

    return normalized


def get_default_config() -> Dict[str, Any]:
    """Erzeugt die plattformspezifische Standard-Konfiguration."""
    ext = ".exe" if IS_WINDOWS else ""

    return {
        "_comment_windows_paths": (
            "BEISPIELE (Windows): Absoluter Pfad -> 'C:/Tools/ffmpeg/bin/ffmpeg.exe' | "
            "Relativer Pfad -> './bin/nvencc64.exe' | System-PATH -> 'ffmpeg.exe'"
        ),
        "_comment_linux_paths": (
            "BEISPIELE (Linux/macOS): Absoluter Pfad -> '/usr/bin/ffmpeg' | "
            "Relativer Pfad -> './bin/ffmpeg' | System-PATH -> 'ffmpeg'"
        ),
        "base_dir": "./",
        "tools": {
            "ffmpeg": f"ffmpeg{ext}",
            "ffprobe": f"ffprobe{ext}",
            "nvencc": f"nvencc64{ext}" if IS_WINDOWS else "nvencc",
            "vmaf": f"vmaf{ext}",
        },
        "default_encoder": _ensure_exe(DEFAULT_ENCODER) if IS_WINDOWS else DEFAULT_ENCODER,
        "default_codec": "av1",
        "default_ai_choice": "2",
    }


def save_config(config: Dict[str, Any], config_path: Path = CONFIG_PATH) -> None:
    """Speichert das Konfigurations-Dict als formatiertes JSON ab."""
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        logger.info(f"Standard-Konfiguration in '{config_path.name}' gespeichert.")
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Konfiguration in '{config_path.name}': {e}")


def load_config(config_file: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Lädt die Konfiguration aus config.json oder erzeugt Standardwerte."""
    default_cfg = get_default_config()

    if not config_file.exists():
        logger.info(f"Keine '{config_file.name}' gefunden. Erstelle Defaults für {PLATFORM_NAME}...")
        save_config(default_cfg, config_file)
        config = default_cfg
    else:
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
                logger.info(f"Konfiguration aus '{config_file.name}' geladen.")
        except Exception as e:
            logger.error(f"Fehler beim Laden von {config_file.name}: {e}. Verwende Standardwerte.")
            config = default_cfg.copy()

    # Inbox, Result und Done liegen auf dem Share; Work darf lokal überschrieben werden.
    base_path = Path(config.get("base_dir", "./")).resolve()
    config["inbox_dir"] = str(base_path / "Inbox")
    configured_work_dir_value = config.get("work_dir")
    configured_work_dir = Path(configured_work_dir_value) if configured_work_dir_value else None
    if configured_work_dir is None:
        work_path = PROJECT_ROOT / "Work"
    elif configured_work_dir.is_absolute():
        work_path = configured_work_dir
    else:
        work_path = (PROJECT_ROOT / configured_work_dir).resolve()
    config["work_dir"] = str(work_path)
    config["result_dir"] = str(base_path / "Result")
    config["done_dir"] = str(base_path / "Done")

    # Sicherstellen, dass Ordner existieren
    for key in ["inbox_dir", "work_dir", "result_dir", "done_dir"]:
        Path(config[key]).mkdir(parents=True, exist_ok=True)

    # Tool-Endungen an das laufende Betriebssystem anpassen.
    if "tools" in config:
        for tool_key, tool_path in config["tools"].items():
            config["tools"][tool_key] = _ensure_exe(tool_path)
    if "default_encoder" in config:
        config["default_encoder"] = resolve_encoder_choice(config["default_encoder"])

    return config


def verify_tools(config: Dict[str, Any]) -> bool:
    """Überprüft die Verfügbarkeit der konfigurierten Tools auf dem System."""
    tools = config.get("tools", {})
    all_valid = True

    logger.info(f"Starte Validierung der Werkzeuge für Betriebssystem: {PLATFORM_NAME}...")

    for tool_name, tool_path in tools.items():
        executable_path = _ensure_exe(tool_path)
        path_obj = Path(executable_path)
        resolved_path = None

        if path_obj.is_absolute() or "/" in executable_path or "\\" in executable_path:
            if path_obj.exists():
                resolved_path = str(path_obj.resolve())
        else:
            found = shutil.which(executable_path)
            if found:
                resolved_path = found

        if resolved_path:
            logger.info(f"  ✓ Tool '{tool_name}' gefunden: {resolved_path}")
        else:
            is_optional = (tool_name == "nvencc" and not IS_WINDOWS)
            if is_optional:
                logger.warning(f"  ⚠️ Optionales Tool '{tool_name}' auf {PLATFORM_NAME} nicht gefunden.")
            else:
                logger.error(f"  ❌ Kritisches Tool '{tool_name}' ('{executable_path}') fehlt!")
                all_valid = False

    return all_valid


# =========================================================================
# 3. INITIALISIERUNG AM ENDE DER DATEI
# Alle Funktionen sind nun definiert, bevor CONFIG und die Exporte erzeugt werden.
# =========================================================================
CONFIG = load_config()