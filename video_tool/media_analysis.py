from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .config import PATHS
    from .utils import logger
except ImportError:  # pragma: no cover - allows direct execution from the module directory
    from config import PATHS
    from utils import logger


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


if __name__ == "__main__":
    import sys

    file_path = Path(sys.argv[1]).expanduser()
    media_info = analyze_media(file_path)
    logger.info("Video resolution: %s", get_video_resolution(media_info))
    logger.info("Video framerate: %s", get_video_framerate(media_info))
    logger.info("Forced subtitles: %s", has_forced_subtitles(media_info.get("subtitle_streams", [])))
    logger.info("Audio details: %s", get_audio_details({"audio_streams": media_info.get("audio_streams", [])}))
