from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from .config import PATHS
    from .utils import logger, HWProfile, detect_hardware
except ImportError:  # pragma: no cover - allows direct execution from the module directory
    from config import PATHS
    from utils import logger, HWProfile, detect_hardware

def get_video_duration(input_path: Path, ffprobe_bin: Optional[Path] = None) -> float:
    """Ermittelt die Gesamtdauer des Videos in Sekunden via ffprobe."""
    ffprobe_exe = str(ffprobe_bin) if ffprobe_bin else str(PATHS.get("ffprobe", "ffprobe"))
    cmd = [
        ffprobe_exe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception as err:
        logger.error("Fehler beim Ermitteln der Videodauer für %s: %s", input_path.name, err)
        return 0.0

def _run_ffprobe(ffprobe_path: Optional[Path | str], input_path: Path | str, *extra_args: str) -> Dict[str, Any]:
    ffprobe = Path(ffprobe_path or PATHS["ffprobe"])
    command = [str(ffprobe), "-v", "error", *extra_args, str(input_path)]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"ffprobe was not found at {ffprobe}.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or str(exc)
        raise RuntimeError(f"ffprobe failed for {input_path}: {stderr}") from exc

    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse ffprobe JSON output: {exc}") from exc


def get_video_resolution(video_info: Dict[str, Any]) -> str:
    stream = (video_info.get("video_streams") or [{}])[0]
    width = stream.get("width", "N/A")
    height = stream.get("height", "N/A")
    return f"{width}x{height}"


def get_video_framerate(video_info: Dict[str, Any]) -> str:
    stream = (video_info.get("video_streams") or [{}])[0]
    return str(stream.get("r_frame_rate", "N/A"))


def get_audio_details(audio_info: Dict[str, Any]) -> str:
    stream = (audio_info.get("audio_streams") or [{}])[0]
    language = stream.get("language", "N/A")
    channels = stream.get("channels", "N/A")
    return f"{language}, {channels} channels"


def analyze_media(input_path: Path, ffprobe_path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Liest Streams und Metadaten der Mediendatei via ffprobe aus."""
    ffprobe_exe = ffprobe_path if ffprobe_path else str(PATHS.get("ffprobe", "ffprobe"))
    cmd = [
        ffprobe_exe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(input_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)

        video_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
        audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        subtitle_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "subtitle"]

        return {
            "format": data.get("format", {}),
            "video_streams": video_streams,
            "audio_streams": audio_streams,
            "subtitle_streams": subtitle_streams,
        }
    except Exception as err:
        logger.error("Fehler bei analyze_media für %s: %s", input_path.name, err)
        return {"video_streams": [], "audio_streams": [], "subtitle_streams": []}


def has_forced_subtitles(subtitle_streams: List[Dict[str, Any]]) -> bool:
    """Prüft, ob in den Untertitelstreams Forced-Flags vorhanden sind."""
    for stream in subtitle_streams:
        disposition = stream.get("disposition", {})
        if disposition.get("forced") == 1:
            return True
    return False


def analyze_noise_and_quality(
    file_path: Optional[Path] = None,
    input_path: Optional[Path] = None,
    ffprobe_path: Optional[str] = None,
    ffmpeg_path: Optional[str] = None,
    work_dir: Optional[Path] = None,
    sample_duration_seconds: int = 10,
    sample_points_pct: Tuple[float, float, float] = (0.50, 0.33, 0.66),
    **kwargs
) -> Dict[str, Any]:
    """
    Multi-Point Rauschanalyse. Vollständig kompatibel mit den Parametern aus main.py.
    """
    target_file = Path(file_path or input_path)
    ffmpeg_exe = Path(ffmpeg_path) if ffmpeg_path else Path(PATHS.get("ffmpeg", "ffmpeg"))
    ffprobe_exe = Path(ffprobe_path) if ffprobe_path else Path(PATHS.get("ffprobe", "ffprobe"))

    if work_dir is None:
        work_dir = target_file.parent / "_temp_analysis"
    else:
        work_dir = Path(work_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    hw: HWProfile = detect_hardware(ffmpeg_exe)

    duration = get_video_duration(target_file, ffprobe_exe)
    if duration <= 0.0:
        logger.warning("Videodauer ungültig. Verwende Standard-Fallback-Werte.")
        return _build_fallback_result(sample_duration_seconds, [])

    sample_points = [int(duration * pct) for pct in sample_points_pct]
    deltas: List[float] = []

    for index, start_sec in enumerate(sample_points):
        raw_path = work_dir / f"sample_{index}_raw.mkv"
        denoised_path = work_dir / f"sample_{index}_denoised.mkv"

        base_cmd = [str(ffmpeg_exe), "-hide_banner", "-loglevel", "error", "-y"]
        if hw.hwaccel_flag:
            base_cmd.extend(["-hwaccel", hw.hwaccel_flag])

        # Raw Sample
        raw_cmd = base_cmd + [
            "-ss", str(start_sec),
            "-i", str(target_file),
            "-t", str(sample_duration_seconds),
            "-map", "0:v:0", "-an", "-sn",
            "-c:v", hw.h264_encoder,
        ] + hw.sample_preset_args + [str(raw_path)]

        # Denoised Sample
        denoise_cmd = base_cmd + [
            "-ss", str(start_sec),
            "-i", str(target_file),
            "-t", str(sample_duration_seconds),
            "-map", "0:v:0", "-an", "-sn",
            "-vf", "hqdn3d=3:3:6:6",
            "-c:v", hw.h264_encoder,
        ] + hw.sample_preset_args + [str(denoised_path)]

        try:
            subprocess.run(raw_cmd, capture_output=True, text=True, check=True)
            subprocess.run(denoise_cmd, capture_output=True, text=True, check=True)

            if raw_path.exists() and denoised_path.exists():
                raw_size = raw_path.stat().st_size
                denoised_size = denoised_path.stat().st_size
                if raw_size > 0:
                    delta_pct = ((raw_size - denoised_size) / raw_size) * 100.0
                    deltas.append(max(0.0, delta_pct))
        except subprocess.CalledProcessError as err:
            logger.warning("Fehler beim Generieren der Samples an Punkt %ds: %s", start_sec, err)

    shutil.rmtree(work_dir, ignore_errors=True)

    if len(deltas) == 3:
        weighted_delta = (deltas[0] * 0.50) + (deltas[1] * 0.25) + (deltas[2] * 0.25)
    elif deltas:
        weighted_delta = sum(deltas) / len(deltas)
    else:
        weighted_delta = 0.0

    weighted_delta = round(weighted_delta, 1)
    logger.info("Multi-Point Pre-Flight Rauschanalyse abgeschlossen. Delta: %.1f%%", weighted_delta)

    result = _evaluate_thresholds(weighted_delta)
    result.update({
        "delta": weighted_delta,
        "sample_duration_seconds": sample_duration_seconds,
        "sample_points": sample_points,
        "hw_profile": hw.name,
    })

    return result


def _evaluate_thresholds(weighted_delta: float) -> Dict[str, Any]:
    """Erzeugt das vollständige Dictionary inklusive aller von main.py erwarteten Keys."""
    if weighted_delta >= 28.0:
        return {
            "noise_level": "heavy",
            "denoise_mode": "heavy",
            "grain_mode": "heavy",
            "quality_value": 94,
            "target_vmaf": 93.5,
            "lower_bound": 92.5,
            "upper_bound": 94.5,
            "filter_args": ["hqdn3d=4:4:8:8"],
            "extra_args": [],
            "noise_detected": True,
            "noise_msg": "Starkes Rauschen erkannt.",
        }
    elif weighted_delta >= 18.0:
        return {
            "noise_level": "medium",
            "denoise_mode": "medium",
            "grain_mode": "medium",
            "quality_value": 95,
            "target_vmaf": 95.0,
            "lower_bound": 94.0,
            "upper_bound": 96.0,
            "filter_args": ["hqdn3d=3:3:6:6"],
            "extra_args": [],
            "noise_detected": True,
            "noise_msg": "Mittleres Rauschen erkannt.",
        }
    elif weighted_delta >= 10.0:
        return {
            "noise_level": "light",
            "denoise_mode": "light",
            "grain_mode": "light",
            "quality_value": 96,
            "target_vmaf": 96.0,
            "lower_bound": 95.0,
            "upper_bound": 97.0,
            "filter_args": ["hqdn3d=1.5:1.5:3:3"],
            "extra_args": [],
            "noise_detected": True,
            "noise_msg": "Leichtes Rauschen erkannt.",
        }
    else:
        return {
            "noise_level": "none",
            "denoise_mode": "off",
            "grain_mode": "off",
            "quality_value": 97,
            "target_vmaf": 97.0,
            "lower_bound": 96.5,
            "upper_bound": 97.5,
            "filter_args": [],
            "extra_args": [],
            "noise_detected": False,
            "noise_msg": "Kein starkes Rauschen erkannt.",
        }


def _build_fallback_result(sample_duration: int, sample_points: List[int]) -> Dict[str, Any]:
    res = _evaluate_thresholds(0.0)
    res.update({
        "delta": 0.0,
        "sample_duration_seconds": sample_duration,
        "sample_points": sample_points,
        "hw_profile": "unknown",
    })
    return res

if __name__ == "__main__":
    import sys

    file_path = Path(sys.argv[1]).expanduser()
    media_info = analyze_media(file_path)
    logger.info("Video resolution: %s", get_video_resolution(media_info))
    logger.info("Video framerate: %s", get_video_framerate(media_info))
    logger.info("Forced subtitles: %s", has_forced_subtitles(media_info.get("subtitle_streams", [])))
    logger.info("Audio details: %s", get_audio_details({"audio_streams": media_info.get("audio_streams", [])}))
