from __future__ import annotations

import logging
import json
import re
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Any, Dict, List
from config import CONFIG, IS_MACOS

try:
    from .paths import PATHS
    from .utils import HWProfile, detect_hardware, logger
except ImportError:  # pragma: no cover
    from paths import PATHS
    from utils import HWProfile, detect_hardware, logger

logger = logging.getLogger("omni_pipeline")

FFMPEG_BIN = CONFIG.get("tools", {}).get("ffmpeg", "ffmpeg")
FFPROBE_BIN = CONFIG.get("tools", {}).get("ffprobe", "ffprobe")
NVENCC_BIN = CONFIG.get("tools", {}).get("nvencc", "nvencc")
VMAF_BIN = CONFIG.get("tools", {}).get("vmaf", "vmaf.exe")

def get_nvenc_base_args(codec: str = "hevc", qvbr: int = 22, extra_args: Optional[Sequence[str]] = None) -> list[str]:
    """Generiert die Basis-Argumente für NVEncC."""
    if codec == "hevc":
        args = [
            "--avhw", "--codec", "hevc", "--profile", "main10", "--tier", "high",
            "--level", "5.1", "--qvbr", str(qvbr), "--output-depth", "10",
            "--preset", "P7", "--multipass", "2pass-full", "--lookahead", "32",
            "--lookahead-level", "3", "--aq", "--aq-temporal", "--ref", "4",
            "--bframes", "4", "--bref-mode", "middle", "--pic-struct",
        ]
    else:
        args = [
            "--avhw", "--codec", "av1", "--profile", "main", "--qvbr", str(qvbr),
            "--output-depth", "10", "--preset", "P7", "--multipass", "2pass-full",
            "--lookahead", "32", "--lookahead-level", "3", "--aq", "--aq-temporal",
            "--ref", "4", "--bframes", "4", "--bref-mode", "middle", "--pic-struct",
        ]

    if extra_args:
        args.extend(extra_args)
    return args


def get_ai_mode_args_nvencc(ai_choice: str = "2", use_nnedi: bool = False) -> list[str]:
    """Gibt die TensorRT / VPP Argumente je nach AI-Modus für NVEncC zurück."""
    if ai_choice == "2":
        return [
            "--colormatrix", "bt2020nc", "--colorprim", "bt2020", "--transfer", "smpte2084",
            "--max-cll", "1000,300", "--master-display", "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
            "--atc-sei", "auto", "--vpp-ngx-truehdr", "contrast=80,saturation=90,middlegray=50,maxluminance=1000",
        ]
    if ai_choice == "3":
        args = [
            "--output-res", "1920x1080,preserve_aspect_ratio=increase",
            "--vpp-resize", "algo=ngx-vsr,vsr-quality=4",
        ]
        if use_nnedi:
            args.extend(["--vpp-nnedi", "quality=slow"])
        return args
    if ai_choice == "4":
        args = [
            "--output-res", "1920x1080,preserve_aspect_ratio=increase",
            "--vpp-resize", "algo=ngx-vsr,vsr-quality=4",
            "--colormatrix", "bt2020nc", "--colorprim", "bt2020", "--transfer", "smpte2084",
            "--max-cll", "1000,300", "--master-display", "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
            "--atc-sei", "auto", "--vpp-ngx-truehdr", "contrast=80,saturation=90,middlegray=50,maxluminance=1000",
        ]
        if use_nnedi:
            args.extend(["--vpp-nnedi", "quality=slow"])
        return args
    return []


def get_ffmpeg_ai_mode_args(
    ai_choice: str = "2",
    use_nnedi: bool = False,
    denoise_filter: Optional[str] = None
) -> Dict[str, Any]:
    """Erstellt die passenden FFmpeg Video-Filter (-vf) und Color-Flags für AI-Choices."""
    vf_filters: List[str] = []
    extra_cmd: List[str] = []

    if denoise_filter:
        vf_filters.append(denoise_filter)

    # Deinterlacing + Upscaling für SD (Choice 3 & 4)
    if ai_choice in ("3", "4"):
        if use_nnedi:
            vf_filters.append("nnedi=field=af:deint=all")
        else:
            vf_filters.append("bwdif=mode=0:parity=-1:deint=0")
        
        vf_filters.append("scale=-2:1080:flags=lanczos")

    # TrueHDR Transformation / Metadaten für Choice 2 & 4
    if ai_choice in ("2", "4"):
        # p010le ist das native 10-bit Format für Apple Silicon Hardware-Encoder
        vf_filters.append("format=p010" if IS_MACOS else "format=yuv420p10le")
        extra_cmd.extend([
            "-color_primaries", "bt2020",
            "-color_trc", "smpte2084",
            "-colorspace", "bt2020nc",
            "-metadata:s:v:0", "mastering_display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
            "-metadata:s:v:0", "max_cll=1000,300"
        ])
    else:
        vf_filters.append("format=nv12" if IS_MACOS else "format=yuv420p")

    return {
        "vf_string": ",".join(vf_filters) if vf_filters else None,
        "extra_cmd": extra_cmd
    }

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

def map_denoise_to_filter(denoise_mode: str, encoder: str = "nvencc") -> Optional[List[str]]:
    """Gibt die passenden Denoise-Argumente für den jeweiligen Encoder zurück.
    Bei 'light' oder 'off' wird None zurückgegeben (kein Filter-Overlay).
    """
    if not denoise_mode or denoise_mode.lower() in {"off", "none"}:
        return None

    mode = denoise_mode.lower()

    if encoder == "nvencc":
        pmd_modes = {
            "light": ["--vpp-pmd", "apply_count=1,strength=10,threshold=25"],
            "medium": ["--vpp-pmd", "apply_count=1,strength=20,threshold=35"],
            "heavy": ["--vpp-pmd", "apply_count=2,strength=30,threshold=50"],
        }
        return pmd_modes.get(mode)
    else:
        ffmpeg_modes = {
            "light": ["-vf", "hqdn3d=2:2:4:4"],
            "medium": ["-vf", "hqdn3d=3:3:6:6"],
            "heavy": ["-vf", "hqdn3d=4:4:8:8"],
        }
        return ffmpeg_modes.get(mode)

def build_encoder_args(
    input_path: Path,
    output_path: Path,
    encoder: str,
    codec: str,
    quality_value: int,
    ai_choice: Optional[str] = None,
    use_nnedi: bool = False,
    denoise_mode: str = "off",
    audio_mode: str = "copy",
    subtitle_burn: bool = False,  # <-- JETZT EXPILZIT ABGEFANGEN
    extra_args: Optional[Sequence[str]] = None,
    is_preflight: bool = False,
    **kwargs  # <-- Fängt alle weiteren Übergabeparameter aus pipeline.py ab!
) -> list[str]:
    """
    Baut das vollständige CLI-Kommando für NVEncC oder FFmpeg auf.
    """
    encoder_clean = encoder.lower().strip()

    # Preflight-Sonderbehandlung: AI-Filter im Preflight deaktivieren
    if is_preflight:
        ai_choice = "1"
    elif not ai_choice:
        ai_choice = str(CONFIG.get("default_ai_choice", "2"))

    # --- NVEncC PFAD ---
    if encoder_clean in ("nvencc", "nvenc"):
        base_args = get_nvenc_base_args(codec=codec, qvbr=quality_value, extra_args=extra_args)
        ai_args = get_ai_mode_args_nvencc(ai_choice=ai_choice, use_nnedi=use_nnedi)
        
        cmd = [
            "nvencc",
            "-i", str(input_path),
            *base_args,
            *ai_args,
            "-o", str(output_path)
        ]
        return cmd

    # --- FFMPEG PFAD ---
    elif encoder_clean == "ffmpeg":
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path)]

        # AI & Video-Filter
        ffmpeg_ai = get_ffmpeg_ai_mode_args(
            ai_choice=ai_choice, 
            use_nnedi=use_nnedi if not is_preflight else False
        )
        
        vf_list = []
        if ffmpeg_ai["vf_string"]:
            vf_list.append(ffmpeg_ai["vf_string"])

        # Subtitle Hardburn falls aktiviert
        if subtitle_burn and not is_preflight:
            vf_list.append(f"subtitles='{input_path}'")

        if vf_list:
            cmd.extend(["-vf", ",".join(vf_list)])

        # Codec-Einstellung
        if IS_MACOS:
            if codec == "av1":
                # Versuche av1_videotoolbox mit passenden VT-Optionen oder nutze libsvtav1
                cmd.extend(["-c:v", "libsvtav1", "-crf", str(quality_value), "-preset", "6"])
            elif codec in ("hevc", "h265"):
                cmd.extend(["-c:v", "hevc_videotoolbox", "-q:v", str(quality_value)])
            else:
                cmd.extend(["-c:v", "h264_videotoolbox", "-q:v", str(quality_value)])
        else:
            if codec == "av1":
                cmd.extend(["-c:v", "libsvtav1", "-crf", str(quality_value), "-preset", "5"])
            elif codec in ("hevc", "h265"):
                cmd.extend(["-c:v", "libx265", "-crf", str(quality_value), "-preset", "slow"])
            else:
                cmd.extend(["-c:v", "libx264", "-crf", str(quality_value), "-preset", "slow"])

        # Audio-Behandlung
        if audio_mode == "aac":
            cmd.extend(["-c:a", "aac", "-b:a", "256k"])
        else:
            cmd.extend(["-c:a", "copy"])

        # Untertitel
        if not subtitle_burn:
            cmd.extend(["-c:s", "copy"])

        # HDR Metadaten (nur beim Haupt-Encode)
        if not is_preflight:
            cmd.extend(ffmpeg_ai["extra_cmd"])

        if extra_args:
            cmd.extend(extra_args)

        cmd.append(str(output_path))
        return cmd

    else:
        raise ValueError(f"Unbekannter Encoder: {encoder}")

def build_vmaf_check_cmd(
    input_path: Path,
    sample_start: int,
    sample_duration: int,
    crf_or_qp: int,
    codec: str,
    denoise_mode: str,
    output_sample_path: Path,
    ffmpeg_bin: Path = Path("ffmpeg")
) -> List[str]:
    """Erstellt ein kleines Test-Sample (ohne Audio) für die VMAF-Messung."""
    hw: HWProfile = detect_hardware(ffmpeg_bin)
    encoder = hw.hevc_encoder if codec.lower() == "hevc" else hw.h264_encoder

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

    denoise_args = map_denoise_to_filter(denoise_mode, encoder="ffmpeg")
    if denoise_args:
        cmd.extend(denoise_args)

    if hw.name == "nvenc":
        cmd.extend(["-rc", "constqp", "-qp", str(crf_or_qp)])
    elif hw.name == "videotoolbox":
        cmd.extend(["-q:v", str(crf_or_qp)])
    else:
        cmd.extend(["-crf", str(crf_or_qp)])

    cmd.append(str(output_sample_path))
    return cmd


def run_command(cmd: list[str], logger: logging.Logger | None = None) -> None:
    if logger is None:
        logger = logging.getLogger("omni_pipeline")

    # Wir suchen nach dem FileHandler im Logger, um Encoder-Output direkt dort reinzuschreiben
    file_handler = next((h for h in logger.handlers if isinstance(h, logging.FileHandler)), None)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace"
    )

    for line in iter(process.stdout.readline, ''):
        if not line:
            break
        clean_line = line.strip()
        if clean_line:
            log_msg = f"[ENCODER] {clean_line}"
            
            # Falls ein FileHandler existiert, schreiben wir es NUR in die Datei
            if file_handler:
                record = logger.makeRecord(
                    logger.name, logging.INFO, "encoding.py", 0, log_msg, (), None
                )
                file_handler.emit(record)
            else:
                # Fallback, falls kein Handler konfiguriert ist
                logger.debug(log_msg)

    process.stdout.close()
    return_code = process.wait()

    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, cmd)