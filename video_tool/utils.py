from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional, Union


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("video_tool")
    if logger.handlers:
        logger.setLevel(level)
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


logger = setup_logging()


def ensure_dir(path: Union[str, Path]) -> Path:
    path_obj = Path(path)
    path_obj.mkdir(parents=True, exist_ok=True)
    return path_obj


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def cut_test_sample(
    input_path: Path | str,
    output_path: Path | str,
    start_seconds: int,
    duration_seconds: int,
    ffmpeg_path: Optional[Path | str] = None,
) -> Path:
    try:
        from .config import PATHS
    except ImportError:
        from config import PATHS

    ffmpeg = Path(ffmpeg_path or PATHS["ffmpeg"])
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
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
        str(duration_seconds),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "18",
        str(output),
    ]

    try:
        subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"ffmpeg was not found at {ffmpeg}.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or str(exc)
        raise RuntimeError(f"ffmpeg sample extraction failed: {stderr}") from exc

    return output