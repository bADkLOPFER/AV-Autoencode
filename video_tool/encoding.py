from __future__ import annotations

import logging
import json
import re
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Any, Dict, List

try:
    from .config import PATHS
    from .utils import HWProfile, detect_hardware, logger
except ImportError:  # pragma: no cover - allows direct execution from the module directory
    from config import PATHS
    from utils import HWProfile, detect_hardware, logger

logger = logging.getLogger(__name__)

def get_nvenc_base_args(codec: str = "hevc", qvbr: int = 22, extra_args: Optional[Sequence[str]] = None) -> list[str]:
    if codec == "hevc":
        args = [
            "--avhw",
            "--codec",
            "hevc",
            "--profile",
            "main10",
            "--tier",
            "high",
            "--level",
            "5.1",
            "--qvbr",
            str(qvbr),
            "--output-depth",
            "10",
            "--preset",
            "P7",
            "--multipass",
            "2pass-full",
            "--lookahead",
            "32",
            "--lookahead-level",
            "3",
            "--aq",
            "--aq-temporal",
            "--ref",
            "4",
            "--bframes",
            "4",
            "--bref-mode",
            "middle",
            "--pic-struct",
        ]
    else:
        args = [
            "--avhw",
            "--codec",
            "av1",
            "--profile",
            "main",
            "--qvbr",
            str(qvbr),
            "--output-depth",
            "10",
            "--preset",
            "P7",
            "--multipass",
            "2pass-full",
            "--lookahead",
            "32",
            "--lookahead-level",
            "3",
            "--aq",
            "--aq-temporal",
            "--ref",
            "4",
            "--bframes",
            "4",
            "--bref-mode",
            "middle",
            "--pic-struct",
        ]

    if extra_args:
        args.extend(extra_args)
    return args


def get_ai_mode_args(ai_choice: str = "1", use_nnedi: bool = False) -> list[str]:
    if ai_choice == "2":
        return [
            "--colormatrix",
            "bt2020nc",
            "--colorprim",
            "bt2020",
            "--transfer",
            "smpte2084",
            "--max-cll",
            "1000,300",
            "--master-display",
            "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
            "--atc-sei",
            "auto",
            "--vpp-ngx-truehdr",
            "contrast=80,saturation=90,middlegray=50,maxluminance=1000",
        ]
    if ai_choice == "3":
        args = [
            "--output-res",
            "1920x1080,preserve_aspect_ratio=increase",
            "--vpp-resize",
            "algo=ngx-vsr,vsr-quality=4",
        ]
        if use_nnedi:
            args.extend(["--vpp-nnedi", "quality=slow"])
        return args
    if ai_choice == "4":
        args = [
            "--output-res",
            "1920x1080,preserve_aspect_ratio=increase",
            "--vpp-resize",
            "algo=ngx-vsr,vsr-quality=4",
            "--colormatrix",
            "bt2020nc",
            "--colorprim",
            "bt2020",
            "--transfer",
            "smpte2084",
            "--max-cll",
            "1000,300",
            "--master-display",
            "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
            "--atc-sei",
            "auto",
            "--vpp-ngx-truehdr",
            "contrast=80,saturation=90,middlegray=50,maxluminance=1000",
        ]
        if use_nnedi:
            args.extend(["--vpp-nnedi", "quality=slow"])
        return args
    return []


def _sanitize_extra_args(encoder: str, extra_args: Optional[Sequence[str]] = None) -> list[str]:
    if not extra_args:
        return []

    unsupported_flags = {"--vmaf-target", "--vmaf-min", "--vmaf-max"}
    sanitized: list[str] = []

    for item in extra_args:
        value = str(item)
        if encoder == "ffmpeg" and value.startswith("--"):
            continue
        if value in unsupported_flags:
            continue
        sanitized.append(value)

    return sanitized


def build_encoder_args(
    input_path: Path | str,
    output_path: Path | str,
    encoder: str = "ffmpeg",
    codec: str = "hevc",
    quality_value: int = 22,
    bitrate_mode: str = "cbr",
    bitrate: int = 5000,
    audio_mode: str = "copy",
    subtitle_burn: bool = False,
    ai_choice: str = "1",
    use_nnedi: bool = False,
    quality_metric: str = "vmaf",
    denoise_mode: str = "off",
    grain_mode: str = "off",
    extra_args: Optional[Sequence[str]] = None,
) -> list[str]:
    input_path = Path(input_path)
    output_path = Path(output_path)

    extra_list = _sanitize_extra_args(encoder, extra_args)
    if quality_metric and quality_metric != "none" and encoder == "ffmpeg":
        extra_list.extend(["--metric", quality_metric])
    if quality_metric and quality_metric != "none" and encoder == "nvencc":
        extra_list.append("--vmaf")
    if denoise_mode and denoise_mode != "off" and encoder == "nvencc":
        extra_list.extend(["--denoise", denoise_mode])
    if grain_mode and grain_mode != "off" and encoder == "nvencc":
        extra_list.extend(["--grain", grain_mode])

    if encoder == "nvencc":
        nvencc_bin = PATHS.get("nvencc")
        if not nvencc_bin:
            raise ValueError("NVEncC path is not configured for this platform.")

        args = [str(nvencc_bin)]
        args.extend(get_nvenc_base_args(codec=codec, qvbr=quality_value, extra_args=extra_list))
        args.extend(get_ai_mode_args(ai_choice=ai_choice, use_nnedi=use_nnedi))
        if subtitle_burn:
            args.extend(["--vpp-subburn", "track=1,forced_subs_only=on"])
        args.extend(["--chapter-copy", "--audio-copy", "-i", str(input_path), "-o", str(output_path)])
        return args

    if encoder == "ffmpeg":
        ffmpeg_bin = PATHS.get("ffmpeg")
        if not ffmpeg_bin:
            raise ValueError("FFmpeg path is not configured.")

        args = [str(ffmpeg_bin), "-hide_banner", "-y", "-i", str(input_path), "-map", "0"]

        if bitrate_mode == "cbr":
            args.extend(["-b:v", str(bitrate)])
        elif bitrate_mode == "vbr":
            args.extend(["-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(max(bitrate * 2, bitrate))])

        args.extend(extra_list)
        args.extend([
            "-c:v",
            "libsvtav1" if codec == "av1" else "libx265",
            "-pix_fmt",
            "yuv420p10le",
            "-preset",
            "4" if codec == "av1" else "slow",
            "-crf",
            str(quality_value),
            "-c:a",
            "copy" if audio_mode == "copy" else "aac",
            "-c:s",
            "copy",
            str(output_path),
        ])

        if subtitle_burn:
            subtitle_filter = f"subtitles='{input_path.as_posix()}':si=0"
            args = [str(ffmpeg_bin), "-hide_banner", "-y", "-i", str(input_path), "-map", "0", "-vf", subtitle_filter]
            args.extend(extra_list)
            args.extend([
                "-c:v",
                "libsvtav1" if codec == "av1" else "libx265",
                "-pix_fmt",
                "yuv420p10le",
                "-preset",
                "4" if codec == "av1" else "slow",
                "-crf",
                str(quality_value),
                "-c:a",
                "copy" if audio_mode == "copy" else "aac",
                "-c:s",
                "copy",
                str(output_path),
            ])

        return args

    raise ValueError(f"Unknown encoder '{encoder}'.")

def map_denoise_to_filter(denoise_mode: str) -> Optional[str]:
    """Überführt den Denoise-Modus in den passenden FFmpeg Video-Filter."""
    filters = {
        "light": "hqdn3d=1.5:1.5:3:3",
        "medium": "hqdn3d=3:3:6:6",
        "heavy": "hqdn3d=4:4:8:8",
    }
    return filters.get(denoise_mode.lower())

def build_vmaf_check_cmd(
    input_path: Path,
    sample_start: int,
    sample_duration: int,
    crf_or_qp: int,
    codec: str,  # "hevc" oder "h264"
    denoise_mode: str,
    output_sample_path: Path,
    vmaf_log_path: Path,
    ffmpeg_bin: Path = Path("ffmpeg")
) -> List[str]:
    """Erstellt ein Test-Sample mit dem Ziel-Encoder und berechnet den VMAF-Score."""
    hw: HWProfile = detect_hardware(ffmpeg_bin)
    
    # 1. Encoder-Wahl basierend auf HWProfile
    if codec.lower() == "hevc":
        encoder = hw.hevc_encoder
    else:
        encoder = hw.h264_encoder

    # 2. Filter-Kette zusammensetzen
    v_filters: List[str] = []
    denoise_filter = map_denoise_to_filter(denoise_mode)
    if denoise_filter:
        v_filters.append(denoise_filter)

    cmd = [str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y"]
    if hw.hwaccel_flag:
        cmd.extend(["-hwaccel", hw.hwaccel_flag])

    cmd.extend([
        "-ss", str(sample_start),
        "-i", str(input_path),
        "-t", str(sample_duration),
        "-map", "0:v:0", "-an", "-sn",
        "-c:v", encoder,
    ])

    # Rate-Control Parameter anpassen
    if hw.name == "nvenc":
        cmd.extend(["-rc", "constqp", "-qp", str(crf_or_qp)])
    elif hw.name == "videotoolbox":
        cmd.extend(["-q:v", str(crf_or_qp)])
    else:
        cmd.extend(["-crf", str(crf_or_qp)])

    if v_filters:
        cmd.extend(["-vf", ",".join(v_filters)])

    cmd.append(str(output_sample_path))
    return cmd


def run_vmaf_score(
    reference_path: Path,
    encoded_sample_path: Path,
    sample_start: int,
    sample_duration: int,
    ffmpeg_bin: Path = Path("ffmpeg")
) -> float:
    """Führt libvmaf-Vergleich zwischen Original-Ausschnitt und Sample durch."""
    vmaf_filter = (
        f"[1:v][0:v]libvmaf="
        f"log_fmt=json:log_path=vmaf_temp.json:n_threads=4"
    )

    cmd = [
        str(ffmpeg_bin), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(sample_start),
        "-i", str(reference_path),
        "-i", str(encoded_sample_path),
        "-t", str(sample_duration),
        "-filter_complex", vmaf_filter,
        "-f", "null", "-"
    ]

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        vmaf_json = Path("vmaf_temp.json")
        if vmaf_json.exists():
            data = json.loads(vmaf_json.read_text(encoding="utf-8"))
            vmaf_json.unlink(missing_ok=True)
            return float(data["pooled_metrics"]["vmaf"]["mean"])
    except Exception as err:
        logger.error("Fehler bei der VMAF-Berechnung: %s", err)
    
    return 0.0


def build_final_encode_cmd(
    input_path: Path,
    output_path: Path,
    quality_val: int,
    codec: str,
    denoise_mode: str,
    ffmpeg_bin: Path = Path("ffmpeg"),
    audio_passthrough: bool = True
) -> List[str]:
    """Baut das finale Haupt-Encoding-Kommando auf."""
    hw: HWProfile = detect_hardware(ffmpeg_bin)
    encoder = hw.hevc_encoder if codec.lower() == "hevc" else hw.h264_encoder

    cmd = [str(ffmpeg_bin), "-hide_banner", "-loglevel", "info", "-y"]
    if hw.hwaccel_flag:
        cmd.extend(["-hwaccel", hw.hwaccel_flag])

    cmd.extend([
        "-i", str(input_path),
        "-c:v", encoder,
    ])

    # Qualitäts-Zuordnung je nach Treiber
    if hw.name == "nvenc":
        cmd.extend(["-preset", "p6", "-rc", "vbr", "-cq", str(quality_val)])
    elif hw.name == "videotoolbox":
        cmd.extend(["-q:v", str(quality_val)])
    else:
        cmd.extend(["-preset", "medium", "-crf", str(quality_val)])

    # Denoise-Filter anhängen
    denoise_filter = map_denoise_to_filter(denoise_mode)
    if denoise_filter:
        cmd.extend(["-vf", denoise_filter])

    # Audio & Subtitle Handling
    if audio_passthrough:
        cmd.extend(["-c:a", "copy", "-c:s", "copy"])
    else:
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-c:s", "copy"])

    cmd.append(str(output_path))
    return cmd

def run_command(cmd_args: Sequence[str | Path]) -> subprocess.CompletedProcess:
    command = [str(item) for item in cmd_args]
    logger.info("Starting process: %s", " ".join(command))

    try:
        return subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Executable could not be found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or str(exc)
        raise RuntimeError(f"Command failed: {stderr}") from exc
