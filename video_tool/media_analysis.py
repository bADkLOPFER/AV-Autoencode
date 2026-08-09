from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .config import PATHS
    from .utils import cut_test_sample, logger
except ImportError:  # pragma: no cover - allows direct execution from the module directory
    from config import PATHS
    from utils import cut_test_sample, logger


def get_video_duration_seconds(file_path: Path | str, ffprobe_path: Optional[Path | str] = None) -> int:
    input_path = Path(file_path).expanduser()
    ffprobe = Path(ffprobe_path or PATHS["ffprobe"])
    command = [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(input_path)]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"ffprobe was not found at {ffprobe}.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or str(exc)
        raise RuntimeError(f"ffprobe failed for {input_path}: {stderr}") from exc

    raw = (completed.stdout or "").strip()
    if not raw:
        raise ValueError("ffprobe did not return a duration value")

    return max(0, int(float(raw)))


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


def analyze_media(file_path: Path | str, ffprobe_path: Optional[Path | str] = None) -> Dict[str, Any]:
    input_path = Path(file_path).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    payload = _run_ffprobe(
        ffprobe_path,
        input_path,
        "-show_streams",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,r_frame_rate,channels,language,disposition",
        "-of",
        "json",
    )

    streams = payload.get("streams") or []
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]

    return {
        "input_path": input_path,
        "streams": streams,
        "video_streams": video_streams,
        "audio_streams": audio_streams,
        "subtitle_streams": subtitle_streams,
        "raw": payload,
    }


def analyze_video(file_path: Path | str, ffprobe_path: Optional[Path | str] = None) -> Dict[str, Any]:
    payload = analyze_media(file_path, ffprobe_path=ffprobe_path)
    return {"video_streams": payload.get("video_streams", [])}


def analyze_audio(file_path: Path | str, ffprobe_path: Optional[Path | str] = None) -> Dict[str, Any]:
    payload = analyze_media(file_path, ffprobe_path=ffprobe_path)
    return {"audio_streams": payload.get("audio_streams", [])}


def analyze_subtitles(file_path: Path | str, ffprobe_path: Optional[Path | str] = None) -> Dict[str, Any]:
    payload = analyze_media(file_path, ffprobe_path=ffprobe_path)
    return {"subtitle_streams": payload.get("subtitle_streams", [])}


def get_video_resolution(video_info: Dict[str, Any]) -> str:
    stream = (video_info.get("video_streams") or [{}])[0]
    width = stream.get("width", "N/A")
    height = stream.get("height", "N/A")
    return f"{width}x{height}"


def get_video_framerate(video_info: Dict[str, Any]) -> str:
    stream = (video_info.get("video_streams") or [{}])[0]
    return str(stream.get("r_frame_rate", "N/A"))


def has_forced_subtitles(streams: List[Dict[str, Any]] | Dict[str, Any]) -> bool:
    if isinstance(streams, dict):
        streams = streams.get("subtitle_streams") or []

    for stream in streams:
        disposition = stream.get("disposition") or {}
        if disposition.get("forced") in (1, "1", True):
            return True
    return False


def get_audio_details(audio_info: Dict[str, Any]) -> str:
    stream = (audio_info.get("audio_streams") or [{}])[0]
    language = stream.get("language", "N/A")
    channels = stream.get("channels", "N/A")
    return f"{language}, {channels} channels"


def analyze_noise_and_quality(
    file_path: Path | str,
    ffprobe_path: Optional[Path | str] = None,
    ffmpeg_path: Optional[Path | str] = None,
    sample_duration_seconds: int = 5,
) -> Dict[str, Any]:
    input_path = Path(file_path).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    try:
        total_duration = get_video_duration_seconds(input_path, ffprobe_path=ffprobe_path)
    except Exception:
        total_duration = 0

    if total_duration <= 0:
        sample_points = [0]
    else:
        sample_points = [max(0, total_duration // 2), max(0, total_duration // 3), max(0, (total_duration * 2) // 3)]

    work_dir = input_path.parent / f"{input_path.stem}_preflight"
    work_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = Path(ffmpeg_path or PATHS["ffmpeg"])

    deltas: List[float] = []
    for index, start_seconds in enumerate(sample_points):
        raw_path = work_dir / f"sample_{index}_raw.mkv"
        denoised_path = work_dir / f"sample_{index}_denoised.mkv"
        cut_test_sample(input_path, raw_path, start_seconds, sample_duration_seconds, ffmpeg_path=ffmpeg)

        denoise_cmd = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(start_seconds),
            "-i",
            str(input_path),
            "-t",
            str(sample_duration_seconds),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-vf",
            "hqdn3d=2.0:2.0:8:8",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            str(denoised_path),
        ]
        try:
            subprocess.run(denoise_cmd, capture_output=True, text=True, check=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"ffmpeg was not found at {ffmpeg}.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip() or str(exc)
            raise RuntimeError(f"ffmpeg denoise sample creation failed: {stderr}") from exc

        if raw_path.exists() and denoised_path.exists():
            raw_size = raw_path.stat().st_size
            denoised_size = denoised_path.stat().st_size
            if raw_size > 0:
                deltas.append((raw_size - denoised_size) / raw_size)

    if not deltas:
        noise_level = "none"
        denoise_mode = "off"
        quality_value = 97
        extra_args: List[str] = []
    else:
        weighted_delta = sum(deltas) / len(deltas)
        if weighted_delta >= 0.50:
            noise_level = "heavy"
            denoise_mode = "heavy"
            quality_value = 94
            extra_args = ["--vpp-pmd", "apply_count=2,strength=35,threshold=45"]
        elif weighted_delta >= 0.30:
            noise_level = "medium"
            denoise_mode = "medium"
            quality_value = 96
            extra_args = ["--vpp-pmd", "apply_count=1,strength=20,threshold=35"]
        elif weighted_delta >= 0.25:
            noise_level = "light"
            denoise_mode = "light"
            quality_value = 95
            extra_args = []
        else:
            noise_level = "none"
            denoise_mode = "off"
            quality_value = 97
            extra_args = []

    target_vmaf = 97.0 if quality_value >= 97 else float(quality_value)
    lower_bound = round(max(90.0, target_vmaf - 1.5), 1)
    upper_bound = round(min(100.0, target_vmaf + 0.5), 1)

    return {
        "denoise_mode": denoise_mode,
        "grain_mode": "off",
        "quality_value": quality_value,
        "target_vmaf": target_vmaf,
        "lower_bound": lower_bound,
        "upper_bound": upper_bound,
        "extra_args": extra_args,
        "noise_detected": noise_level != "none",
        "noise_level": noise_level,
        "delta": round(sum(deltas) / len(deltas), 3) if deltas else 0.0,
        "sample_duration_seconds": sample_duration_seconds,
        "sample_points": sample_points,
    }


if __name__ == "__main__":
    import sys

    file_path = Path(sys.argv[1]).expanduser()
    media_info = analyze_media(file_path)
    logger.info("Video resolution: %s", get_video_resolution(media_info))
    logger.info("Video framerate: %s", get_video_framerate(media_info))
    logger.info("Forced subtitles: %s", has_forced_subtitles(media_info.get("subtitle_streams", [])))
    logger.info("Audio details: %s", get_audio_details({"audio_streams": media_info.get("audio_streams", [])}))
    