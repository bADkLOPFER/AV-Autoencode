from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from .config import CONFIG
    from .paths import WORK_DIR, PATHS
    from .encoding import build_encoder_args, run_command
    from .utils import _clamp, ensure_dir, logger
except ImportError:  # pragma: no cover
    from config import CONFIG
    from paths import WORK_DIR, PATHS
    from encoding import build_encoder_args, run_command
    from utils import _clamp, ensure_dir, logger

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
            qvbr = int(min(max_qvbr, qvbr + 2))
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

    max_crf = 28 if codec == "av1" else 24
    crf = 24 if codec == "av1" else 22
    steps = [2, 1]
    attempts: List[Dict[str, Any]] = []
    last_vmaf: Optional[float] = None

    for step in steps:
        vmaf = float(default_quality_estimator(plan, crf, requested_quality))
        attempts.append({"crf": crf, "vmaf": round(vmaf, 3)})
        last_vmaf = vmaf

        if lower_bound <= vmaf <= upper_bound:
            return {"quality_value": crf, "attempts": attempts, "vmaf": vmaf}

        if vmaf > upper_bound:
            crf -= step
        elif vmaf < lower_bound:
            crf += step

        crf = int(_clamp(crf, 1, max_crf))

    if last_vmaf is not None and last_vmaf > upper_bound and crf < max_crf:
        while crf < max_crf:
            crf = int(min(max_crf, crf + 1))
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
    if encoder == "ffmpeg":
        result = find_quality_value_ffmpeg(plan, codec=codec, requested_quality=requested_quality)
        return int(result["quality_value"])

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
    ffmpeg_bin = Path(ffmpeg_path) if ffmpeg_path else PATHS.get("ffmpeg", Path("ffmpeg"))
    ffprobe_bin = Path(ffprobe_path) if ffprobe_path else PATHS.get("ffprobe", Path("ffprobe"))
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

def run_vmaf_score(
    reference_path: Path,
    encoded_sample_path: Path,
    sample_start: int = 0,
    sample_duration: int = 10,
    ffmpeg_bin: Path = Path("ffmpeg_vmaf")
) -> float:
    """Führt den libvmaf-Vergleich durch, resetted Timestamps und erzwingt 10-Bit YUV."""
    work_dir = Path(encoded_sample_path).parent
    vmaf_json = work_dir / "vmaf_temp.json"

    if vmaf_json.exists():
        vmaf_json.unlink(missing_ok=True)

    # 2. Filter-String mit f-string und sauberem Relativpfad für den Work-Ordner
    vmaf_filter = (
        "[0:v]setpts=PTS-STARTPTS,format=yuv420p10le[ref];"
        "[1:v]setpts=PTS-STARTPTS,format=yuv420p10le[dist];"
        "[dist][ref]scale2ref[dist_sc][ref_sc];"
        "[dist_sc][ref_sc]libvmaf=log_fmt=json:log_path=vmaf_temp.json:n_threads=4"
    )

    cmd = [
        str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(reference_path),
        "-i", str(encoded_sample_path),
        "-filter_complex", vmaf_filter,
        "-f", "null", "-"
    ]

    try:
        subprocess.run(cmd, cwd=str(work_dir), capture_output=True, text=True, check=True)
        if vmaf_json.exists():
            data = json.loads(vmaf_json.read_text(encoding="utf-8"))
            vmaf_json.unlink(missing_ok=True)
            return float(data["pooled_metrics"]["vmaf"]["mean"])
    except Exception as err:
        logger.error("Fehler bei der VMAF-Berechnung: %s", err)

    return 0.0

def calibrate_quality_vmaf(
    input_path: Path,
    work_dir: Path,
    noise_plan: Dict[str, Any],
    codec: str,
    encoder: str,
    initial_q: int,
) -> Tuple[int, float]:
    """Erstellt ein Test-Sample an der Peak-Szene, kalibriert VMAF und misst die Encoding-Dauer."""
    peak_time = float(noise_plan.get("peak_timestamp_sec", 0.0))
    denoise_mode = noise_plan.get("denoise_mode", "off")

    print(f"[>] VMAF-Kalibrierung: Analysiere Peak-Szene bei {peak_time:.2f}s (Denoise: {denoise_mode})")
    logger.debug("VMAF-Kalibrierung gestartet für %s bei %.2fs mit Denoise '%s'", input_path.name, peak_time, denoise_mode)
    
    ensure_dir(work_dir)

    ffmpeg_bin = PATHS.get("ffmpeg", Path("ffmpeg"))
    ffmpeg_vmaf_bin = ffmpeg_bin
    if str(CONFIG.get("platform", "")).lower() not in ("windows", "win32"):
        ffmpeg_vmaf_bin = PATHS.get("ffmpeg_vmaf", Path("ffmpeg_vmaf"))

    ref_sample = work_dir / f"{input_path.stem}_ref_peak_10s.mkv"

    cut_cmd = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", str(peak_time),
        "-i", str(input_path),
        "-t", "10",
        "-map", "0:v:0",
        "-c:v", "copy",
        "-avoid_negative_ts", "make_zero",
        str(ref_sample),
    ]

    logger.debug("FFmpeg Referenz-Extraktionsbefehl: %s", " ".join(cut_cmd))
    print(f"    -> Erstelle 10s Referenz-Sample im Arbeitsverzeichnis...")

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

    for attempt in range(1, 4):
        test_encoded = work_dir / f"{input_path.stem}_test_q{current_q}.mkv"

        test_cmd = build_encoder_args(
            input_path=ref_sample,
            output_path=test_encoded,
            encoder=encoder,
            codec=codec,
            quality_value=current_q,
            ai_choice="1",
            use_nnedi=False,
            denoise_mode=denoise_mode,
            extra_args=noise_plan.get("extra_args", []),
        )

        logger.debug("Test-Encode Befehl (Versuch %d, Q=%d): %s", attempt, current_q, " ".join(test_cmd))
        print(f"    -> Teste Durchlauf {attempt}/3 mit Qualitätsstufe Q={current_q}...")

        try:
            t_start = time.time()
            run_command(test_cmd)
            last_sample_duration = time.time() - t_start
        except Exception as exc:
            logger.warning("Test-Encode fehlgeschlagen für Q=%d: %s", current_q, exc)
            break

        print(f"    -> Messung VMAF-Score für Q={current_q}...")
        vmaf_score = run_vmaf_score(
            reference_path=ref_sample,
            encoded_sample_path=test_encoded,
            sample_start=0,
            sample_duration=10,
            ffmpeg_bin=Path(ffmpeg_vmaf_bin),
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

        if vmaf_score > upper_bound:
            current_q += 2
        else:
            current_q -= 2

        current_q = max(14, min(current_q, 36))

    print(f"[OK] Kalibrierung abgeschlossen. Optimaler Qualitätswert: Q={best_q}")
    return best_q, last_sample_duration

def has_forced_subtitles(subtitle_streams: List[Dict[str, Any]]) -> bool:
    """Prüft, ob erzwungene Untertitelspuren vorhanden sind."""
    for stream in subtitle_streams:
        disposition = stream.get("disposition", {})
        if disposition.get("forced") == 1:
            return True
    return False