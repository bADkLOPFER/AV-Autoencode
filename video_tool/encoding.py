from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Sequence

try:
    from .config import PATHS
    from .utils import logger
except ImportError:  # pragma: no cover - allows direct execution from the module directory
    from config import PATHS
    from utils import logger


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
    extra_args: Optional[Sequence[str]] = None,
) -> list[str]:
    input_path = Path(input_path)
    output_path = Path(output_path)

    if encoder == "nvencc":
        # 1. Den echten Pfad zur nvencc64.exe als ERTES Element setzen!
        nvencc_bin = PATHS.get("nvencc")
        if not nvencc_bin:
            raise ValueError("NVEncC path is not configured for this platform.")

        args = [str(nvencc_bin)]

        # 2. Danach die Basis-Args anhängen
        args.extend(get_nvenc_base_args(codec=codec, qvbr=quality_value, extra_args=list(extra_args or [])))
        args.extend(get_ai_mode_args(ai_choice=ai_choice, use_nnedi=use_nnedi))
        if subtitle_burn:
            args.extend(["--vpp-subburn", "track=1,forced_subs_only=on"])
        args.extend(["--chapter-copy", "--audio-copy", "-i", str(input_path), "-o", str(output_path)])
        return args

    if encoder == "ffmpeg":
        # 1. Den echten Pfad zur ffmpeg-Binary als ERSTES Element setzen!
        ffmpeg_bin = PATHS.get("ffmpeg")
        if not ffmpeg_bin:
            raise ValueError("FFmpeg path is not configured.")

        args = [str(ffmpeg_bin), "-hide_banner", "-y", "-i", str(input_path), "-map", "0"]

        if bitrate_mode == "cbr":
            args.extend(["-b:v", str(bitrate)])
        elif bitrate_mode == "vbr":
            args.extend(["-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(max(bitrate * 2, bitrate))])

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
