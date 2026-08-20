from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from config import CONFIG, IS_WINDOWS
    from .encoding import build_encoder_args, run_command
    from .utils import _clamp, ensure_dir, logger
except ImportError:  # pragma: no cover
    from config import CONFIG, IS_WINDOWS
    from encoding import build_encoder_args, run_command
    from utils import _clamp, ensure_dir, logger

logger = logging.getLogger(__name__)

FFMPEG_BIN = CONFIG.get("tools", {}).get("ffmpeg", "ffmpeg")
FFPROBE_BIN = CONFIG.get("tools", {}).get("ffprobe", "ffprobe")
NVENCC_BIN = CONFIG.get("tools", {}).get("nvencc") or ("nvencc64.exe" if IS_WINDOWS else "nvencc")
VMAF_BIN = CONFIG.get("tools", {}).get("vmaf", "vmaf.exe" if IS_WINDOWS else "vmaf")
WORK_DIR = CONFIG["work_dir"]
PROGRESSIVE_DEINTERLACE_FILTER = "bwdif=mode=0:parity=-1:deint=0,setparams=field_mode=prog"

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
    ffmpeg_bin: Path = Path("ffmpeg"),
    sample_duration: float = 5.0,
    quality_value: int = 28,
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
        "-an", "-sn",
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
        enc_cmd = [
            str(ffmpeg_bin),
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(ref_sample),
            "-c:v", "libx265",          # Zuverlässiger Software-Codec für den Test
            "-preset", "ultrafast",     # Maximaler Speed für den Preflight
            "-crf", str(quality_value), # Stabiles CRF statt hardwareabhängigem Rate-Control
            "-an",                      # Kein Audio
            "-sn",                      # Keine Untertitel
            str(test_encoded),
        ]
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
        logger.error("Delta-Messung an Position %.2fs fehlgeschlagen: %s", timestamp, exc)
        if ref_sample.exists():
            ref_sample.unlink()
        if test_encoded.exists():
            test_encoded.unlink()
        return 0.0

# --- Parameter & Quality Estimator Logic ---

def default_quality_estimator(plan: Dict[str, Any], candidate_quality: int, requested_quality: int) -> float:
    target_vmaf = float(plan.get("target_vmaf", 97.0))
    lower_bound = float(plan.get("lower_bound", target_vmaf - 0.5))
    upper_bound = float(plan.get("upper_bound", target_vmaf + 0.5))

    delta = (candidate_quality - requested_quality) / 10.0
    estimate = target_vmaf + delta * 0.35

    if plan.get("noise_level") == "heavy":
        estimate -= 0.2
    elif plan.get("noise_level") == "medium":
        estimate -= 0.1
    elif plan.get("noise_level") == "light":
        estimate -= 0.05

    return _clamp(estimate, lower_bound - 0.7, upper_bound + 0.7)


def find_quality_value_nvenc(
    plan: Dict[str, Any],
    codec: str = "hevc",
    encoder: str = "nvencc",
    requested_quality: int = 22,
    estimator: Optional[Callable[[Dict[str, Any], int, int], float]] = None,
) -> Dict[str, Any]:
    target_vmaf = float(plan.get("target_vmaf", 97.0))
    lower_bound = float(plan.get("lower_bound", target_vmaf - 0.5))
    upper_bound = float(plan.get("upper_bound", target_vmaf + 0.5))

    max_qvbr = 34 if codec == "av1" else 30
    qvbr = 26 if codec == "av1" else 22
    steps = [4, 2, 1]
    attempts: List[Dict[str, Any]] = []
    last_vmaf: Optional[float] = None
    estimate_fn = estimator or default_quality_estimator

    for step in steps:
        vmaf = float(estimate_fn(plan, qvbr, requested_quality))
        attempts.append({"qvbr": qvbr, "vmaf": round(vmaf, 3)})
        last_vmaf = vmaf

        if lower_bound <= vmaf <= upper_bound:
            return {"quality_value": qvbr, "attempts": attempts, "vmaf": vmaf}

        if vmaf > upper_bound:
            qvbr += step
        elif vmaf < lower_bound:
            qvbr -= step

        qvbr = int(_clamp(qvbr, 1, max_qvbr))

    if last_vmaf is not None and last_vmaf > upper_bound and qvbr < max_qvbr:
        while qvbr < max_qvbr:
            qvbr = int(min(max_qvbr, qvbr + steps[-1]))  # mit der feinsten Schrittweite weitersuchen, statt zurück auf 2 zu springen
            vmaf = float(estimate_fn(plan, qvbr, requested_quality))
            attempts.append({"qvbr": qvbr, "vmaf": round(vmaf, 3)})
            last_vmaf = vmaf
            if lower_bound <= vmaf <= upper_bound:
                return {"quality_value": qvbr, "attempts": attempts, "vmaf": vmaf}

    closest = min(attempts, key=lambda item: (abs(float(item["vmaf"]) - target_vmaf), -float(item["vmaf"])))
    return {"quality_value": int(closest["qvbr"]), "attempts": attempts, "vmaf": float(closest["vmaf"])}


def find_quality_value_ffmpeg(
    plan: Dict[str, Any],
    codec: str = "hevc",
    requested_quality: int = 22,
) -> Dict[str, Any]:
    target_vmaf = float(plan.get("target_vmaf", 97.0))
    lower_bound = float(plan.get("lower_bound", target_vmaf - 0.5))
    upper_bound = float(plan.get("upper_bound", target_vmaf + 0.5))

    max_crf = 34 if codec == "av1" else 30
    crf = 27 if codec == "av1" else 22
    steps = [4, 2, 1]
    attempts: List[Dict[str, Any]] = []
    last_vmaf: Optional[float] = None

    for step in steps:
        vmaf = float(default_quality_estimator(plan, crf, requested_quality))
        attempts.append({"crf": crf, "vmaf": round(vmaf, 3)})
        last_vmaf = vmaf

        if lower_bound <= vmaf <= upper_bound:
            return {"quality_value": crf, "attempts": attempts, "vmaf": vmaf}

        if vmaf > upper_bound:
            crf += step  # höherer CRF = niedrigere Qualität/VMAF, wie beim QVBR-Pendant
        elif vmaf < lower_bound:
            crf -= step

        crf = int(_clamp(crf, 1, max_crf))

    if last_vmaf is not None and last_vmaf > upper_bound and crf < max_crf:
        while crf < max_crf:
            crf = int(min(max_crf, crf + steps[-1]))
            vmaf = float(default_quality_estimator(plan, crf, requested_quality))
            attempts.append({"crf": crf, "vmaf": round(vmaf, 3)})
            last_vmaf = vmaf
            if lower_bound <= vmaf <= upper_bound:
                return {"quality_value": crf, "attempts": attempts, "vmaf": vmaf}

    closest = min(attempts, key=lambda item: (abs(float(item["vmaf"]) - target_vmaf), -float(item["vmaf"])))
    return {"quality_value": int(closest["crf"]), "attempts": attempts, "vmaf": float(closest["vmaf"])}


def recommend_quality_value(
    plan: Dict[str, Any],
    codec: str = "hevc",
    encoder: str = "nvencc",
    requested_quality: int = 22,
) -> int:
    if encoder.lower().strip() in ("ffmpeg", "qsv", "vcenc", "vceenc"):
        result = find_quality_value_ffmpeg(plan, codec=codec, requested_quality=requested_quality)
        return int(result["quality_value"])
    else:
        result = find_quality_value_nvenc(plan, codec=codec, encoder=encoder, requested_quality=requested_quality)
        return int(result["quality_value"])

def analyze_noise_and_quality(
    file_path: Path,
    bitrate_info: Optional[Dict[str, Any]] = None,
    ffprobe_path: Optional[str] = None,
    ffmpeg_path: Optional[str] = None,
    work_dir: Optional[Path] = None,
    encoder: str = "ffmpeg",
    codec: str = "av1",
    logger=None
) -> Dict[str, Any]:
    """Führt eine Multi-Point Kompressions-Delta-Analyse (33%, 66%, Peak) durch."""
    if logger is None:
        logger = logging.getLogger("omni_pipeline")
    ffmpeg_bin = Path(ffmpeg_path) if ffmpeg_path else FFMPEG_BIN
    ffprobe_bin = Path(ffprobe_path) if ffprobe_path else FFPROBE_BIN
    target_work_dir = Path(work_dir) if work_dir else WORK_DIR

    if not bitrate_info:
        bitrate_info = get_bitrate_info(file_path, ffprobe_bin=ffprobe_bin)

    duration = get_video_duration(file_path, ffprobe_bin=ffprobe_bin)
    peak_time = _safe_float(bitrate_info.get("peak_timestamp_sec"), duration * 0.5 if duration > 0 else 0.0)
    peak_kbps = _safe_int(bitrate_info.get("peak_kbps"), 0)

    t_33 = round(duration * 0.33, 2) if duration > 30 else 5.0
    t_66 = round(duration * 0.66, 2) if duration > 30 else round(duration * 0.8, 2)
    t_peak = peak_time

    logger.info("Starte Kompressions-Delta-Rauschanalyse (33%%, 66%%, Peak)...")

    delta_33 = _measure_delta_at(file_path, t_33, target_work_dir, ffmpeg_bin)
    delta_66 = _measure_delta_at(file_path, t_66, target_work_dir, ffmpeg_bin)
    delta_peak = _measure_delta_at(file_path, t_peak, target_work_dir, ffmpeg_bin)

    delta_weighted = (delta_33 * 0.25) + (delta_66 * 0.25) + (delta_peak * 0.50)

    logger.info(
        "Delta-Ergebnisse: 33%%=%.1f%%, 66%%=%.1f%%, Peak=%.1f%% -> Gewichtet: %.1f%%",
        delta_33, delta_66, delta_peak, delta_weighted
    )

    # Schwellenwerte analog zur PowerShell-Logik
    if delta_weighted > 40.0:
        noise_level = "heavy"
        denoise_mode = "heavy"
        target_vmaf = 93.0
    elif delta_weighted > 30.0:
        noise_level = "medium"
        denoise_mode = "medium"
        target_vmaf = 94.5
    elif delta_weighted > 20.0:
        noise_level = "light"
        denoise_mode = "light"
        target_vmaf = 95.5
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

def detect_interlacing(
    input_path: Path,
    ffmpeg_path: Optional[str] = None,
    ffprobe_path: Optional[str] = None,
) -> bool:
    """
    Ermittelt anhand der Stream-Metadaten und eines kurzen idet-Samples,
    ob der erste Videostream interlaced ist.

    Die Stream-Metadaten sind die zuverlässigste und billigste Entscheidung
    für PAFF/MBAFF. idet bleibt der Fallback für Dateien ohne brauchbares
    field_order-Metadatum oder bei fehlerhaften Containern.
    """
    ffmpeg_bin = str(ffmpeg_path or FFMPEG_BIN)
    ffprobe_bin = str(ffprobe_path or FFPROBE_BIN)
    interlaced_orders = {"tt", "bb", "tb", "bt"}

    # field_order is present for normal interlaced, PAFF and MBAFF streams.
    try:
        probe_cmd = [
            ffprobe_bin, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=field_order", "-of", "default=nw=1:nk=1",
            str(input_path),
        ]
        field_order = subprocess.run(
            probe_cmd, capture_output=True, text=True, check=True
        ).stdout.strip().lower()
        if field_order in interlaced_orders:
            return True
        if field_order == "progressive":
            return False
    except (OSError, subprocess.CalledProcessError):
        field_order = ""

    # FFmpeg has changed the surrounding wording of idet output over time;
    # parse the individual counters instead of relying on one fixed sentence.
    duration = get_video_duration(input_path, Path(ffprobe_bin))
    start = 0.0 if duration <= 8 else min(60.0, max(0.0, duration * 0.25))
    cmd = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "info", "-ss", str(start),
        "-i", str(input_path), "-t", "8", "-an", "-sn", "-vf", "idet",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", check=False,
        )
    except OSError as exc:
        logger.warning("Interlace-Erkennung konnte FFmpeg nicht starten: %s", exc)
        return False

    import re
    stats = re.findall(
        r"TFF:\s*(\d+)\s+BFF:\s*(\d+)\s+"
        r"Progressive:\s*(\d+)\s+Undetermined:\s*(\d+)",
        result.stderr or "",
        flags=re.IGNORECASE,
    )
    if not stats:
        return False

    # Prefer the last report, which is idet's final multi-frame summary.
    tff, bff, progressive, _undetermined = map(int, stats[-1])
    interlaced_frames = tff + bff
    classified_frames = interlaced_frames + progressive
    return bool(
        classified_frames
        and interlaced_frames >= 3
        and interlaced_frames / classified_frames >= 0.10
    )

def analyze_media(
    file_path: Path,
    ffprobe_path: Optional[str] = None,
    ai_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Analysiert Streams und passt den AI-Modus für kleine Videos an."""
    ffprobe_bin = Path(ffprobe_path) if ffprobe_path else FFPROBE_BIN
    selected_ai_mode = str(ai_mode or CONFIG.get("default_ai_choice", "2"))

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
        return {
            "video_streams": [],
            "audio_streams": [],
            "subtitle_streams": [],
            "bitrate_info": {},
            "ai_mode": selected_ai_mode,
        }

    streams = data.get("streams", [])
    v_streams = [s for s in streams if s.get("codec_type") == "video"]
    a_streams = [s for s in streams if s.get("codec_type") == "audio"]
    s_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    bitrate_info = get_bitrate_info(file_path, ffprobe_bin=ffprobe_bin)
    if v_streams and _safe_int(v_streams[0].get("height"), 0) < 730:
        try:
            numeric_ai_mode = int(selected_ai_mode)
        except (TypeError, ValueError):
            numeric_ai_mode = 2
        if numeric_ai_mode < 3:
            numeric_ai_mode += 2
            selected_ai_mode = str(numeric_ai_mode)
            logger.info(
                "Vertikale Auflösung unter 730 erkannt; AI-Modus auf %s erhöht.",
                selected_ai_mode,
            )
    
    return {
        "video_streams": v_streams,
        "audio_streams": a_streams,
        "subtitle_streams": s_streams,
        "format": data.get("format", {}),
        "bitrate_info": bitrate_info,
        "is_interlaced": detect_interlacing(file_path, ffprobe_path=str(ffprobe_bin)),
        "ai_mode": selected_ai_mode,
    }


def calculate_vmaf_score(
    reference_sample: Path,
    encoded_sample: Path,
    vmaf_model_path: Optional[str] = None,
    force_8bit: bool = True,  # Garantiert identische Bit-Tiefe für VMAF
    is_interlaced: bool = False,  # Optionales Deinterlacing für interlaced Material
) -> float:
    """
    Berechnet den VMAF-Score plattformunabhängig über das externe vmaf-CLI.
    Konvertiert die Samples in temporäre Y4M-Dateien mit identischem Pixelformat.
    """
    vmaf_bin = str(VMAF_BIN)
    ffmpeg_bin = str(FFMPEG_BIN)

    ref_y4m = reference_sample.with_suffix(".y4m")
    dist_y4m = encoded_sample.with_suffix(".y4m")

    # Erzwinge einheitliches Pixelformat (z. B. yuv420p), um "bitdepths do not match"-Fehler zu vermeiden
    pix_fmt_args = ["-pix_fmt", "yuv420p"] if force_8bit else ["-strict", "-1"]
    vf_args = []

    if is_interlaced:
        vf_args = ["-vf", "bwdif=mode=0:parity=-1:deint=0,setparams=field_mode=prog"]
        
    try:
        # 1. Referenz-Sample in Y4M konvertieren
        subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(reference_sample),
                *vf_args,
                *pix_fmt_args,
                str(ref_y4m)
            ],
            check=True
        )

        # 2. Test-Encode (Distorted) in Y4M konvertieren
        subprocess.run(
            [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(encoded_sample),
                *vf_args,
                *pix_fmt_args,
                str(dist_y4m)
            ],
            check=True
        )

        # Temporäre JSON-Datei für das Messergebnis
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp_file:
            json_output_path = Path(tmp_file.name)

        # 3. VMAF CLI aufrufen
        cmd = [
            vmaf_bin,
            "-r", str(ref_y4m),
            "-d", str(dist_y4m),
            "-o", str(json_output_path),
            "--json"
        ]

        if vmaf_model_path:
            cmd.extend(["--model", f"path={vmaf_model_path}"])

        subprocess.run(cmd, capture_output=True, text=True, check=True)

        # 4. JSON verarbeiten
        if json_output_path.exists():
            with open(json_output_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            json_output_path.unlink(missing_ok=True)
            return round(float(data["pooled_metrics"]["vmaf"]["mean"]), 2)

    except subprocess.CalledProcessError as e:
        logger.error("VMAF CLI-Aufruf fehlgeschlagen: %s", e.stderr if hasattr(e, 'stderr') else e)
    except Exception as e:
        logger.error("Fehler bei der VMAF-Analyse: %s", e)
    finally:
        # Temporäre Y4M-Dateien im Work-Dir wieder aufräumen
        if ref_y4m.exists():
            ref_y4m.unlink(missing_ok=True)
        if dist_y4m.exists():
            dist_y4m.unlink(missing_ok=True)

    return 0.0


def cleanup_vmaf_samples(work_dir: Path, input_stem: str) -> None:
    """Entfernt die temporären VMAF-Samples eines Jobs aus dem Arbeitsordner."""
    patterns = (
        f"{input_stem}_ref_peak_*.mkv",
        f"{input_stem}_test_q*.mkv",
        f"{input_stem}_ref_peak_*.y4m",
        f"{input_stem}_test_q*.y4m",
    )
    for pattern in patterns:
        for sample_path in work_dir.glob(pattern):
            try:
                sample_path.unlink()
            except OSError as exc:
                logger.warning("VMAF-Sample konnte nicht gelöscht werden (%s): %s", sample_path, exc)

def calibrate_quality_vmaf(
    input_path: Path,
    work_dir: Path,
    noise_plan: Dict[str, Any],
    codec: str,
    encoder: str,
    initial_q: int,
    is_interlaced: bool = False,
) -> Tuple[int, float]:
    """Erstellt ein Test-Sample an der Peak-Szene, kalibriert VMAF und misst die Encoding-Dauer."""
    peak_time = float(noise_plan.get("peak_timestamp_sec", 0.0))
    denoise_mode = noise_plan.get("denoise_mode", "off")

    print(f"[>] VMAF-Kalibrierung: Analysiere Peak-Szene bei {peak_time:.2f}s (Denoise: {denoise_mode})")
    logger.debug("VMAF-Kalibrierung gestartet für %s bei %.2fs mit Denoise '%s'", input_path.name, peak_time, denoise_mode)
    
    ensure_dir(work_dir)

    ffmpeg_bin = FFMPEG_BIN
    ref_sample = work_dir / f"{input_path.stem}_ref_peak_20s.mkv"

    cut_cmd = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", str(peak_time),
        "-i", str(input_path),
        "-t", "20",
        "-map", "0:v:0",
        "-an", "-sn",
    ]

    if is_interlaced:
        cut_cmd.extend([
            "-vf", PROGRESSIVE_DEINTERLACE_FILTER,
            "-c:v", "ffv1",
        ])
    else:
        cut_cmd.extend(["-c:v", "copy"])

    cut_cmd.extend([
        "-avoid_negative_ts", "make_zero",
        str(ref_sample),
    ])
        
    logger.debug("FFmpeg Referenz-Extraktionsbefehl: %s", " ".join(cut_cmd))
    print(f"    -> Erstelle 20s Referenz-Sample im Arbeitsverzeichnis...")

    try:
        run_command(cut_cmd)
    except Exception as exc:
        logger.warning("Referenz-Sample konnte nicht erstellt werden: %s. Nutze Startwert %d.", exc, initial_q)
        return initial_q, 0.0

    target_vmaf = float(noise_plan.get("target_vmaf", 95.0))
    lower_bound = float(noise_plan.get("lower_bound", target_vmaf - 0.8))
    upper_bound = float(noise_plan.get("upper_bound", target_vmaf + 0.8))

    current_q = initial_q
    best_q = current_q
    min_delta = 999.0
    last_sample_duration = 0.0
    prev_q: Optional[int] = None
    prev_vmaf: Optional[float] = None
    max_attempts = 6
    default_step = 2

    for attempt in range(1, max_attempts + 1):
        test_encoded = work_dir / f"{input_path.stem}_test_q{current_q}.mkv"

        test_cmd = build_encoder_args(
            input_path=ref_sample,
            output_path=test_encoded,
            encoder=encoder,
            codec=codec,
            quality_value=current_q,
            use_nnedi=False,
            is_preflight=True
        )

        logger.debug("Test-Encode Befehl (Versuch %d, Q=%d): %s", attempt, current_q, " ".join(test_cmd))
        print(f"    -> Teste Durchlauf {attempt}/{max_attempts} mit Qualitätsstufe Q={current_q}...")

        try:
            t_start = time.time()
            run_command(test_cmd)
            last_sample_duration = time.time() - t_start
        except Exception as exc:
            logger.warning("Test-Encode fehlgeschlagen für Q=%d: %s", current_q, exc)
            break

        print(f"    -> Messung VMAF-Score für Q={current_q}...")
        
        # Aufruf des entkoppelten VMAF CLI Tools:
        vmaf_score = calculate_vmaf_score(
            reference_sample=ref_sample,
            encoded_sample=test_encoded,
            is_interlaced=False
        )

        if vmaf_score <= 0.0:
            logger.warning("VMAF-Messung liefert kein Ergebnis. Breche Kalibrierung ab.")
            break

        print(f"    -> Ergebnis Versuch {attempt}: Q={current_q} -> VMAF: {vmaf_score:.2f} (Ziel: {lower_bound:.1f} - {upper_bound:.1f})")

        delta = abs(vmaf_score - target_vmaf)
        if delta < min_delta:
            min_delta = delta
            best_q = current_q

        if lower_bound <= vmaf_score <= upper_bound:
            print(f"[OK] VMAF-Zielwert im Toleranzbereich getroffen bei Q={current_q}!")
            return current_q, last_sample_duration

        # Schrittweite aus der bisher gemessenen Steigung (VMAF pro Q) schätzen,
        # damit große Abweichungen (z.B. VMAF 99.8 bei Ziel ~96.5) nicht an
        # trägen 2er-Schritten scheitern, bevor die Versuche aufgebraucht sind.
        step = default_step
        if prev_q is not None and prev_vmaf is not None and current_q != prev_q:
            slope = (prev_vmaf - vmaf_score) / (current_q - prev_q)
            if abs(slope) > 1e-3:
                gap = vmaf_score - upper_bound if vmaf_score > upper_bound else lower_bound - vmaf_score
                step = max(1, min(int(round(abs(gap / slope))), 8))

        prev_q, prev_vmaf = current_q, vmaf_score

        if vmaf_score > upper_bound:
            current_q += step
        else:
            current_q -= step

        current_q = max(14, min(current_q, 36))

    print(f"[OK] Kalibrierung abgeschlossen. Optimaler Qualitätswert: Q={best_q}")
    return best_q, last_sample_duration

def get_forced_subtitle_track(subtitle_streams: List[Dict[str, Any]]) -> Optional[int]:
    """Returns the zero-based subtitle-stream position of the first forced track.

    Manche Mux-Tools setzen das FlagForced-Bit im Container nicht, sondern
    markieren die Spur nur über den Titel/Tag (z.B. "Forced", "Signs"). Daher
    wird zusätzlich zur Disposition auch der Titel-Tag als Fallback geprüft.
    """
    for position, stream in enumerate(subtitle_streams):
        if _safe_int(stream.get("disposition", {}).get("forced", 0), 0) == 1:
            return position

    for position, stream in enumerate(subtitle_streams):
        title = str(stream.get("tags", {}).get("title", "")).lower()
        if "forced" in title:
            return position

    return None


def has_forced_subtitles(subtitle_streams: List[Dict[str, Any]]) -> bool:
    """Prüft, ob erzwungene Untertitelspuren vorhanden sind."""
    return get_forced_subtitle_track(subtitle_streams) is not None