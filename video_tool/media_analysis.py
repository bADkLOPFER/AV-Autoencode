from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from .config import PATHS
    from .encoding import build_encoder_args
    from .utils import ensure_dir, logger
except ImportError:  # pragma: no cover
    from config import PATHS
    from encoding import build_encoder_args
    from utils import ensure_dir, logger

logger = logging.getLogger(__name__)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Konvertiert Werte sicher in float (fängt 'N/A' ab)."""
    if val is None or val == "N/A":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    """Konvertiert Werte sicher in int (fängt 'N/A' ab)."""
    if val is None or val == "N/A":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def get_video_duration(file_path: Path, ffprobe_bin: Path = Path("ffprobe")) -> float:
    """Ermittelt die Gesamtdauer des Videos in Sekunden."""
    cmd = [
        str(ffprobe_bin),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return _safe_float(res.stdout.strip())
    except Exception as exc:
        logger.warning("Dauer konnte nicht ermittelt werden: %s", exc)
        return 0.0


def get_bitrate_info(
    file_path: Path,
    ffprobe_bin: Path = Path("ffprobe"),
    window_seconds: float = 30.0,
) -> Dict[str, Any]:
    """Analysiert die Quelldatei via Paket-Scan und liefert Bitratendaten."""
    info: Dict[str, Any] = {"avg_kbps": 0, "peak_kbps": 0, "peak_timestamp_sec": 0.0}

    cmd_avg = [
        str(ffprobe_bin),
        "-v", "error",
        "-show_entries", "format=bit_rate",
        "-of", "json",
        str(file_path),
    ]
    try:
        res = subprocess.run(cmd_avg, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        bitrate_raw = data.get("format", {}).get("bit_rate")
        info["avg_kbps"] = int(_safe_int(bitrate_raw) / 1000)
    except Exception as exc:
        logger.debug("Header-Bitrate nicht lesbar: %s", exc)

    logger.info("  -> Ermittle Bitraten-Peak via FFprobe Paket-Scan (30s Fenster)...")
    cmd_packets = [
        str(ffprobe_bin),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=size,duration_time,pts_time",
        "-of", "json",
        str(file_path),
    ]

    try:
        res = subprocess.run(cmd_packets, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        packets = data.get("packets", [])

        if packets:
            current_bytes = 0
            current_duration = 0.0
            max_bps = 0.0
            peak_time = 0.0
            window: List[Tuple[int, float, float]] = []

            for pkt in packets:
                size = _safe_int(pkt.get("size"), 0)
                duration = _safe_float(pkt.get("duration_time"), 0.04167)
                pts = _safe_float(pkt.get("pts_time"), 0.0)

                if size <= 0:
                    continue

                window.append((size, duration, pts))
                current_bytes += size
                current_duration += duration

                while current_duration > window_seconds and len(window) > 1:
                    old_size, old_dur, _ = window.pop(0)
                    current_bytes -= old_size
                    current_duration -= old_dur

                if current_duration > 0:
                    instant_bps = (current_bytes * 8) / current_duration
                    if instant_bps > max_bps:
                        max_bps = instant_bps
                        peak_time = window[0][2]

            info["peak_kbps"] = int(max_bps / 1000)
            info["peak_timestamp_sec"] = round(peak_time, 2)

    except Exception as exc:
        logger.warning("Paket-Bitraten-Analyse fehlgeschlagen: %s", exc)

    if info["avg_kbps"] == 0 and info["peak_kbps"] > 0:
        info["avg_kbps"] = int(info["peak_kbps"] * 0.6)

    return info


def _measure_delta_at(
    file_path: Path,
    timestamp: float,
    work_dir: Path,
    encoder: str = "ffmpeg",
    codec: str = "av1",
    ffmpeg_bin: Path = Path("ffmpeg"),
    sample_duration: float = 5.0,
) -> float:
    """Misst die Komprimierbarkeit (Delta-Bitrate) an einem Zeitstempel."""
    ensure_dir(work_dir)
    stamp_id = int(timestamp)
    ref_sample = work_dir / f"temp_delta_ref_{stamp_id}.mkv"
    test_encoded = work_dir / f"temp_delta_test_{stamp_id}.mkv"

    # 1. 5s Stream-Copy des Originals erstellen
    cut_cmd = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", str(timestamp),
        "-i", str(file_path),
        "-t", str(sample_duration),
        "-map", "0:v:0",
        "-c:v", "copy",
        "-avoid_negative_ts", "make_zero",
        str(ref_sample),
    ]

    try:
        subprocess.run(cut_cmd, capture_output=True, text=True, check=True)
        ref_size = ref_sample.stat().st_size
        if ref_size <= 0:
            return 0.0

        # 2. Benchmark-Encode (Q=26, ungefiltert)
        enc_cmd = build_encoder_args(
            input_path=ref_sample,
            output_path=test_encoded,
            encoder=encoder,
            codec=codec,
            quality_value=26,
            ai_choice="1",
            use_nnedi=False,
            denoise_mode="off",
        )
        subprocess.run(enc_cmd, capture_output=True, text=True, check=True)
        test_size = test_encoded.stat().st_size

        # 3. Kompressions-Delta in % berechnen
        delta = (test_size / ref_size) * 100.0

        # Aufräumen
        if ref_sample.exists():
            ref_sample.unlink()
        if test_encoded.exists():
            test_encoded.unlink()

        logger.info(
            "  -> Delta-Messung bei %.2fs: Ref=%d KB, Test=%d KB -> Delta = %.1f%%",
            timestamp, ref_size // 1024, test_size // 1024, delta
        )
        return round(delta, 1)

    except Exception as exc:
        logger.warning("Delta-Messung an Position %.2fs fehlgeschlagen: %s", timestamp, exc)
        if ref_sample.exists():
            ref_sample.unlink()
        if test_encoded.exists():
            test_encoded.unlink()
        return 0.0


def analyze_noise_and_quality(
    file_path: Path,
    bitrate_info: Optional[Dict[str, Any]] = None,
    ffprobe_path: Optional[str] = None,
    ffmpeg_path: Optional[str] = None,
    work_dir: Optional[Path] = None,
    encoder: str = "ffmpeg",
    codec: str = "av1",
) -> Dict[str, Any]:
    """Führt eine Multi-Point Kompressions-Delta-Analyse (33%, 66%, Peak) durch."""
    ffmpeg_bin = Path(ffmpeg_path) if ffmpeg_path else PATHS.get("ffmpeg", Path("ffmpeg"))
    ffprobe_bin = Path(ffprobe_path) if ffprobe_path else PATHS.get("ffprobe", Path("ffprobe"))
    target_work_dir = work_dir if work_dir else PATHS.get("results", Path(".")) / "Work"

    if not bitrate_info:
        bitrate_info = get_bitrate_info(file_path, ffprobe_bin=ffprobe_bin)

    duration = get_video_duration(file_path, ffprobe_bin=ffprobe_bin)
    peak_time = _safe_float(bitrate_info.get("peak_timestamp_sec"), duration * 0.5 if duration > 0 else 0.0)
    peak_kbps = _safe_int(bitrate_info.get("peak_kbps"), 0)

    t_33 = round(duration * 0.33, 2) if duration > 30 else 5.0
    t_66 = round(duration * 0.66, 2) if duration > 30 else round(duration * 0.8, 2)
    t_peak = peak_time

    logger.info("Starte Kompressions-Delta-Rauschanalyse (33%%, 66%%, Peak)...")

    delta_33 = _measure_delta_at(file_path, t_33, target_work_dir, encoder, codec, ffmpeg_bin)
    delta_66 = _measure_delta_at(file_path, t_66, target_work_dir, encoder, codec, ffmpeg_bin)
    delta_peak = _measure_delta_at(file_path, t_peak, target_work_dir, encoder, codec, ffmpeg_bin)

    delta_weighted = (delta_33 * 0.25) + (delta_66 * 0.25) + (delta_peak * 0.50)

    logger.info(
        "Delta-Ergebnisse: 33%%=%.1f%%, 66%%=%.1f%%, Peak=%.1f%% -> Gewichtet: %.1f%%",
        delta_33, delta_66, delta_peak, delta_weighted
    )

    # Schwellenwerte analog zur PowerShell-Logik
    if delta_weighted > 45.0:
        noise_level = "heavy"
        denoise_mode = "medium"
        target_vmaf = 93.0
    elif delta_weighted > 30.0:
        noise_level = "medium"
        denoise_mode = "light"
        target_vmaf = 94.5
    else:
        noise_level = "low"
        denoise_mode = "off"
        target_vmaf = 96.5

    lower_bound = round(target_vmaf - 0.8, 1)
    upper_bound = round(target_vmaf + 0.8, 1)

    return {
        "noise_level": noise_level,
        "denoise_mode": denoise_mode,
        "target_vmaf": target_vmaf,
        "lower_bound": lower_bound,
        "upper_bound": upper_bound,
        "peak_timestamp_sec": t_peak,
        "peak_kbps": peak_kbps,
        "delta_weighted": round(delta_weighted, 1),
        "extra_args": [],
    }


def analyze_media(file_path: Path, ffprobe_path: Optional[str] = None) -> Dict[str, Any]:
    """Analysiert Streams, Auflösung und Bitraten der Mediendatei."""
    ffprobe_bin = Path(ffprobe_path) if ffprobe_path else PATHS.get("ffprobe", Path("ffprobe"))

    cmd = [
        str(ffprobe_bin),
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(file_path),
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
    except Exception as exc:
        logger.error("ffprobe analysis failed for %s: %s", file_path, exc)
        return {"video_streams": [], "audio_streams": [], "subtitle_streams": [], "bitrate_info": {}}

    streams = data.get("streams", [])
    v_streams = [s for s in streams if s.get("codec_type") == "video"]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]
    s_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    bitrate_info = get_bitrate_info(file_path, ffprobe_bin=ffprobe_bin)

    return {
        "video_streams": v_streams,
        "audio_streams": a_streams,
        "subtitle_streams": s_streams,
        "format": data.get("format", {}),
        "bitrate_info": bitrate_info,
    }


def has_forced_subtitles(subtitle_streams: List[Dict[str, Any]]) -> bool:
    """Prüft, ob erzwungene Untertitelspuren vorhanden sind."""
    for stream in subtitle_streams:
        disposition = stream.get("disposition", {})
        if disposition.get("forced") == 1:
            return True
    return False