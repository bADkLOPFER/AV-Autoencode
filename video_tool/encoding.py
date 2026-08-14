from __future__ import annotations

import logging
import json
import re
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Any, Dict, List

try:
    from .paths import PATHS
    from .utils import HWProfile, detect_hardware, logger
except ImportError:  # pragma: no cover
    from paths import PATHS
    from utils import HWProfile, detect_hardware, logger

logger = logging.getLogger(__name__)

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

def get_ai_mode_args(ai_choice: str = "1", use_nnedi: bool = False) -> list[str]:
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
    input_path: Path | str,
    output_path: Path | str,
    encoder: str = "ffmpeg",
    codec: str = "hevc",
    quality_value: int = 22,
    bitrate_mode: str = "cbr",  # Wieder aufgenommen, um main.py kompatibel zu halten
    bitrate: int = 5000,        # Wieder aufgenommen
    audio_mode: str = "copy",
    subtitle_burn: bool = False,
    ai_choice: str = "1",
    use_nnedi: bool = False,
    quality_metric: str = "vmaf",
    denoise_mode: str = "off",
    extra_args: Optional[Sequence[str]] = None,
) -> list[str]:
    """Baut das finale Kommando für den Haupt-Encode unter Berücksichtigung von Hardware und Filtern."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    extra_list = _sanitize_extra_args(encoder, extra_args)

    if encoder == "nvencc":
        nvencc_bin = PATHS.get("nvencc")
        if not nvencc_bin:
            raise ValueError("NVEncC path is not configured for this platform.")

        if quality_metric and quality_metric != "none":
            extra_list.append("--vmaf")
            
        denoise_args = map_denoise_to_filter(denoise_mode, encoder="nvencc")
        if denoise_args:
            extra_list.extend(denoise_args)

        args = [str(nvencc_bin)]
        args.extend(get_nvenc_base_args(codec=codec, qvbr=quality_value, extra_args=extra_list))
        args.extend(get_ai_mode_args(ai_choice=ai_choice, use_nnedi=use_nnedi))
        
        if subtitle_burn:
            args.extend(["--vpp-subburn", "track=1,forced_subs_only=on"])
            
        args.extend(["--chapter-copy", "--audio-copy", "-i", str(input_path), "-o", str(output_path)])
        return args

    elif encoder == "ffmpeg":
        ffmpeg_bin = PATHS.get("ffmpeg")
        if not ffmpeg_bin:
            raise ValueError("FFmpeg path is not configured.")

        hw: HWProfile = detect_hardware(ffmpeg_bin)
        args = [str(ffmpeg_bin), "-hide_banner", "-loglevel", "info", "-y"]
        
        if getattr(hw, "mode", "").lower() != "cpu" and hw.hwaccel_flag:
            args.extend(["-hwaccel", hw.hwaccel_flag])

        args.extend(["-i", str(input_path), "-map", "0"])

        # FFmpeg Videofilter Kette zusammenbauen (-vf)
        v_filters: List[str] = []
        denoise_args = map_denoise_to_filter(denoise_mode, encoder="ffmpeg")
        if denoise_args:
            v_filters.append(denoise_args[1])
            
        if subtitle_burn:
            escaped_path = input_path.as_posix().replace(":", r"\:")
            v_filters.append(f"subtitles='{escaped_path}':si=0")

        if v_filters:
            args.extend(["-vf", ",".join(v_filters)])

        # Video-Codec und Qualitätssteuerung (HWProfile aware)
        target_v_encoder = "libsvtav1" if codec == "av1" else hw.hevc_encoder
        args.extend(["-c:v", target_v_encoder, "-pix_fmt", "yuv420p10le"])

        if target_v_encoder == "libsvtav1":
            args.extend(["-preset", "4", "-crf", str(quality_value)])
        else:
            if hw.name == "nvenc":
                # GEÄNDERT: constqp funktioniert für Test-Samples ohne Bitraten-Vorgabe zuverlässig
                args.extend(["-preset", "p6", "-rc", "constqp", "-qp", str(quality_value)])
            elif hw.name == "videotoolbox":
                args.extend(["-q:v", str(quality_value)])
            elif hw.name == "qsv":
                args.extend(["-preset", "slow", "-global_quality", str(quality_value)])
            elif hw.name == "amf":
                args.extend(["-quality", "quality", "-rc", "cqp", "-qp_p", str(quality_value)])
            else:
                args.extend(["-preset", "slow", "-crf", str(quality_value)])

        #if quality_metric and quality_metric != "none":
        #    extra_list.extend(["--metric", quality_metric]) if isinstance(quality_metric, str) else [] # safe append
            
        args.extend(extra_list)

        # Audio & Ausgabe
        args.extend([
            "-c:a", "copy" if audio_mode == "copy" else "aac",
            "-c:s", "copy",
            str(output_path),
        ])

        return args

    raise ValueError(f"Unknown encoder '{encoder}'.")

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


def run_vmaf_score(
    reference_path: Path,
    encoded_sample_path: Path,
    sample_start: int = 0,
    sample_duration: int = 10,
    ffmpeg_bin: Path = Path("ffmpeg")
) -> float:
    """Führt den libvmaf-Vergleich durch, resetted Timestamps und erzwingt 10-Bit YUV."""
    work_dir = Path(encoded_sample_path).parent
    vmaf_json = work_dir / "vmaf_temp.json"

    if vmaf_json.exists():
        vmaf_json.unlink(missing_ok=True)

    # 2. Filter-String mit f-string und sauberem Relativpfad für den Work-Ordner
    vmaf_filter = (
        "[0:v]setpts=PTS-STARTPTS,format=yuv420p[ref];"
        "[1:v]setpts=PTS-STARTPTS,format=yuv420p[dist];"
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


def run_command(cmd_args: Sequence[str | Path]) -> subprocess.CompletedProcess:
    """Führt den Subprocess aus und fängt Standardfehler sauber ab."""
    command = [str(item) for item in cmd_args]
    logger.info("Starting process: %s", " ".join(command))

    try:
        return subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Executable could not be found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or str(exc)
        raise RuntimeError(f"Command failed: {stderr}") from exc
