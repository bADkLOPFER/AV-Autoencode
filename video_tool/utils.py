from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, List, Union
import sys

logger = logging.getLogger("video_tool")

#try:
#    from .config import PATHS
#except ImportError:  # pragma: no cover - allows direct execution from the module directory
#    from config import PATHS

def _clamp(value: float, min_val: float, max_val: float) -> float:
    """Begrenzt einen Wert auf den Bereich [min_val, max_val]."""
    return max(min_val, min(value, max_val))

@dataclass
class HWProfile:
    name: str  # nvenc, videotoolbox, qsv, amf, cpu
    hwaccel_flag: Optional[str]
    h264_encoder: str
    hevc_encoder: str
    sample_preset_args: List[str]


@lru_cache(maxsize=1)
def detect_hardware(ffmpeg_bin: Optional[str | Path] = None) -> HWProfile:
    """
    Ermittelt beim ersten Aufruf die bestmögliche Hardware-Beschleunigung von FFmpeg.
    Ergebnis wird via @lru_cache gespeichert, um wiederholte Subprocess-Aufrufe zu vermeiden.
    """
    if ffmpeg_bin is None:
        ffmpeg_str = str(PATHS.get("ffmpeg", "ffmpeg"))
    else:
        ffmpeg_str = str(ffmpeg_bin)

    # Encoders auslesen
    encoders: List[str] = []
    try:
        res = subprocess.run(
            [ffmpeg_str, "-encoders"], capture_output=True, text=True, check=True
        )
        encoders = [line.split()[1] for line in res.stdout.splitlines() if len(line.split()) >= 2]
    except Exception as err:
        logger.warning("Fehler beim Auslesen der FFmpeg-Encoder: %s", err)

    # HWAccels auslesen
    hwaccels: List[str] = []
    try:
        res = subprocess.run(
            [ffmpeg_str, "-hwaccels"], capture_output=True, text=True, check=True
        )
        lines = res.stdout.splitlines()
        start_index = next((i + 1 for i, line in enumerate(lines) if "Hardware acceleration methods:" in line), 0)
        hwaccels = [line.strip() for line in lines[start_index:] if line.strip()]
    except Exception as err:
        logger.warning("Fehler beim Auslesen der HWAccels: %s", err)

    # 1. NVIDIA NVENC (CUDA / NVDEC)
    if "h264_nvenc" in encoders:
        logger.info("Hardware-Beschleunigung erkannt: NVIDIA NVENC")
        return HWProfile(
            name="nvenc",
            hwaccel_flag="cuda" if "cuda" in hwaccels else None,
            h264_encoder="h264_nvenc",
            hevc_encoder="hevc_nvenc",
            sample_preset_args=["-preset", "p1", "-rc", "constqp", "-qp", "18"],
        )

    # 2. Apple Silicon / macOS (VideoToolbox)
    if "h264_videotoolbox" in encoders:
        logger.info("Hardware-Beschleunigung erkannt: Apple Silicon / VideoToolbox")
        return HWProfile(
            name="videotoolbox",
            hwaccel_flag="videotoolbox" if "videotoolbox" in hwaccels else None,
            h264_encoder="h264_videotoolbox",
            hevc_encoder="hevc_videotoolbox",
            sample_preset_args=["-q:v", "65"],
        )

    # 3. Intel QuickSync (QSV)
    if "h264_qsv" in encoders:
        logger.info("Hardware-Beschleunigung erkannt: Intel QuickSync (QSV)")
        return HWProfile(
            name="qsv",
            hwaccel_flag="qsv" if "qsv" in hwaccels else None,
            h264_encoder="h264_qsv",
            hevc_encoder="hevc_qsv",
            sample_preset_args=["-preset", "veryfast", "-global_quality", "18"],
        )

    # 4. AMD AMF
    if "h264_amf" in encoders:
        logger.info("Hardware-Beschleunigung erkannt: AMD AMF")
        return HWProfile(
            name="amf",
            hwaccel_flag=None,
            h264_encoder="h264_amf",
            hevc_encoder="hevc_amf",
            sample_preset_args=["-quality", "speed", "-rc", "cqp", "-qp_p", "18"],
        )

    # 5. Software CPU Fallback
    logger.info("Keine dedizierte GPU-Beschleunigung gefunden -> Nutze CPU (libx264/libx265)")
    return HWProfile(
        name="cpu",
        hwaccel_flag=None,
        h264_encoder="libx264",
        hevc_encoder="libx265",
        sample_preset_args=["-preset", "ultrafast", "-crf", "18"],
    )

def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("video_tool")
    if logger.handlers:
        logger.setLevel(level)
        return logger

    handler = logging.StreamHandler(sys.stdout)
    # Exaktes Format [INFO] / [WARN] wie im PowerShell-Skript
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    # WARNINGs als WARN anzeigen
    logging.addLevelName(logging.WARNING, "WARN")

    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


logger = setup_logging()


def format_hms(seconds: int) -> str:
    """Wandelt Sekunden in HH:MM:SS Formate um (z. B. 8103s -> 02:15:03)."""
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def ensure_dir(path: Union[str, Path]) -> Path:
    path_obj = Path(path)
    path_obj.mkdir(parents=True, exist_ok=True)
    return path_obj
