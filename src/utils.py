from __future__ import annotations

import logging
import subprocess
import ctypes
import platform
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, List, Union, Set
import sys

try:
    from .config import CONFIG
except ImportError: 
    from config import CONFIG

logger = logging.getLogger("omni_pipeline")

FFMPEG_BIN = CONFIG.get("tools", {}).get("ffmpeg", "ffmpeg")

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
    Ermittelt plattformübergreifend und laufzeitsicher die bestmögliche
    Hardware-Beschleunigung (NVIDIA, Intel QSV, VAAPI, Apple VideoToolbox)
    oder fällt sauber auf den CPU-Modus (SVT-AV1 / libx264) zurück.
    """
    if ffmpeg_bin is None:
        ffmpeg_str = str(FFMPEG_BIN)
    else:
        ffmpeg_str = str(ffmpeg_bin)

    os_name = platform.system()

    # 1. FFmpeg-Capabilities (-hwaccels und -encoders) auslesen
    hwaccels: Set[str] = set()
    encoders: Set[str] = set()
    
    try:
        # HWAccels abfragen
        res_hw = subprocess.run([ffmpeg_str, "-hwaccels"], capture_output=True, text=True, check=True)
        for line in res_hw.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("Hardware"):
                hwaccels.add(line.lower())

        # Encoders abfragen
        res_enc = subprocess.run([ffmpeg_str, "-encoders"], capture_output=True, text=True, check=True)
        for line in res_enc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and any(char in parts[0].lower() for char in ["v", "s"]):
                encoders.add(parts[1].lower())
    except Exception as err:
        logger.warning("Fehler beim Abfragen der FFmpeg-Schnittstellen: %s", err)

    # 2. Plattform- & Laufzeit-Validierungen (Gibt es die Hardware physisch?)

    # --- APPLE SILICON / MACOS (VideoToolbox) ---
    if os_name == "Darwin" and "videotoolbox" in hwaccels and "h264_videotoolbox" in encoders:
        logger.info("Hardware-Beschleunigung aktiv: Apple VideoToolbox")
        return HWProfile(
            name="videotoolbox",
            hwaccel_flag="videotoolbox",
            h264_encoder="h264_videotoolbox",
            hevc_encoder="hevc_videotoolbox",
            sample_preset_args=["-allow_sw", "1"],
        )

    # --- NVIDIA (NVENC / CUDA) ---
    cuda_runtime_ok = False
    if "cuda" in hwaccels or "h264_nvenc" in encoders:
        if os_name == "Linux":
            # Im Linux/LXC-Container prüfen, ob der Treiber physisch ladbar ist
            try:
                ctypes.CDLL("libcuda.so.1")
                cuda_runtime_ok = True
            except OSError:
                cuda_runtime_ok = False
        else:
            cuda_runtime_ok = True

    if cuda_runtime_ok and "h264_nvenc" in encoders:
        logger.info("Hardware-Beschleunigung aktiv: NVIDIA NVENC (CUDA)")
        return HWProfile(
            name="nvencc",
            hwaccel_flag="cuda",
            h264_encoder="h264_nvenc",
            hevc_encoder="hevc_nvenc",
            sample_preset_args=["-preset", "p4"],
        )

    # --- INTEL QUICK SYNC (QSV) ---
    qsv_runtime_ok = False
    if "qsv" in hwaccels or "h264_qsv" in encoders:
        if os_name == "Linux":
            # QSV benötigt Render-Nodes (z.B. iGPU Passthrough in Proxmox)
            qsv_runtime_ok = Path("/dev/dri/renderD128").exists()
        else:
            qsv_runtime_ok = True

    if qsv_runtime_ok and "h264_qsv" in encoders:
        logger.info("Hardware-Beschleunigung aktiv: Intel QuickSync (QSV)")
        return HWProfile(
            name="qsv",
            hwaccel_flag="qsv",
            h264_encoder="h264_qsv",
            hevc_encoder="hevc_qsv",
            sample_preset_args=["-preset", "medium"],
        )

    # --- VAAPI (Intel / AMD unter Linux) ---
    vaapi_runtime_ok = False
    if "vaapi" in hwaccels or "h264_vaapi" in encoders:
        if os_name == "Linux":
            vaapi_runtime_ok = Path("/dev/dri/renderD128").exists()

    if vaapi_runtime_ok and "h264_vaapi" in encoders:
        logger.info("Hardware-Beschleunigung aktiv: VAAPI (Intel/AMD)")
        return HWProfile(
            name="vaapi",
            hwaccel_flag="vaapi",
            h264_encoder="h264_vaapi",
            hevc_encoder="hevc_vaapi",
            sample_preset_args=["-preset", "medium"],
        )

    # --- UNIVERSALER CPU-FALLBACK ---
    logger.info("Keine nutzbare GPU-Beschleunigung gefunden -> Nutze reinen CPU-Modus (SVT-AV1 / libx264)")
    return HWProfile(
        name="cpu",
        hwaccel_flag=None,
        h264_encoder="libx264",
        hevc_encoder="libx265",
        sample_preset_args=["-preset", "4", "-crf", "24"],
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

def calculate_adjusted_speed_factor(test_speed_factor: float, ai_choice: str = "none") -> float:
    """
    Berechnet den finalen Geschwindigkeitsfaktor unter Berücksichtigung von 
    Test-Speed, AI/Filter-Option und der zugrundeliegenden Hardware-Architektur.
    Dient der genauen Hochrechnung der verbleibenden Restzeit (ETA) im Frontend.
    """
    hw_profile = detect_hardware()

    # 1. Hardware-spezifische Basis-Gewichtung relativ zu NVEncC
    hw_multipliers = {
        "nvenc": 1.0,          # Referenz (z.B. RTX-Grafikkarte)
        "videotoolbox": 0.95,  # Apple Silicon Media Engine
        "qsv": 0.85,           # Intel QuickSync
        "amf": 0.80,           # AMD AMF
        "cpu": 0.15            # Reine Software-Berechnung (deutlich langsamer)
    }
    
    hw_factor = hw_multipliers.get(hw_profile.name.lower(), 1.0)
    
    # 2. Dämpfungsfaktor durch gewählte AI-Modi / Zusatzfilter
    ai_penalties = {
        "none": 1.0,
        "light": 0.90,
        "medium": 0.85,
        "heavy": 0.80,
        "nnedi_slow": 0.35
    }
    
    ai_factor = ai_penalties.get(ai_choice.lower(), 1.0)
    
    # 3. Zusammenführung der Faktoren mit dem gemessenen Test-Speed
    adjusted_factor = test_speed_factor * hw_factor * ai_factor
    
    # Sicherheitsgrenzen: Mindestens 0.05x Speed, maximal 50x Speed
    return _clamp(adjusted_factor, 0.05, 50.0)

def estimate_total_duration(source_duration_seconds: float, adjusted_speed_factor: float) -> float:
    """
    Berechnet die geschätzte Gesamtdauer des Encodiervorgangs in Minuten.
    """
    if adjusted_speed_factor <= 0:
        return 0.0
    
    return source_duration_seconds / adjusted_speed_factor