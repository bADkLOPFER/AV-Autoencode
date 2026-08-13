from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from .utils import _clamp
except ImportError: 
    from utils import _clamp

SCRIPT_DIR = Path(__file__).resolve().parent

def detect_platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


PROJECT_ROOT = SCRIPT_DIR.parent

PLATFORM = detect_platform()
IS_WINDOWS = PLATFORM == "windows"
IS_MACOS = PLATFORM == "macos"
IS_LINUX = PLATFORM == "linux"


if IS_WINDOWS:
    PATHS = {
        "ffmpeg": PROJECT_ROOT / "FFMPeg" / "ffmpeg.exe",
        "ffprobe": PROJECT_ROOT / "FFMPeg" / "ffprobe.exe",
        "nvencc": PROJECT_ROOT / "NVEncC" / "nvencc64.exe",
        "results": PROJECT_ROOT / "Results",
    }
elif IS_MACOS:
    PATHS = {
        "ffmpeg": Path("/opt/homebrew/bin/ffmpeg"),
        "ffprobe": Path("/opt/homebrew/bin/ffprobe"),
        "nvencc": None,
        "results": SCRIPT_DIR / "Results",
    }
else:
    PATHS = {
        "ffmpeg": Path("/usr/bin/ffmpeg"),
        "ffprobe": Path("/usr/bin/ffprobe"),
        "nvencc": Path("/usr/local/bin/nvencc"),
        "results": SCRIPT_DIR / "Results",
    }


DEFAULT_ENCODER = "nvencc" if IS_WINDOWS else "ffmpeg"
ENCODER_CHOICES = ("ffmpeg", "nvencc")

DEFAULT_WEB_PORT = 8265

def resolve_encoder_choice(requested: Optional[str]) -> str:
    if requested is None:
        return DEFAULT_ENCODER

    normalized = requested.lower()
    if normalized not in ENCODER_CHOICES:
        raise ValueError(f"Unsupported encoder '{requested}'. Expected one of {ENCODER_CHOICES}.")

    if normalized == "nvencc" and not IS_WINDOWS:
        raise ValueError("NVEncC is only supported on Windows. Please use ffmpeg on macOS or Linux.")

    return normalized


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


CONFIG = {
    "platform": PLATFORM,
    "encoder": DEFAULT_ENCODER,
    "paths": PATHS,
    "debug_args": True,
}
WORKFLOW_DEFAULTS = {
    "hw_accel_mode": "auto",          # "auto", "nvencc", oder "cpu"
    "default_codec": "av1",           # "av1" oder "hevc"
    "default_true_hdr": True,         # TrueHDR standardmäßig aktivieren
    "disk_space_multiplier": 1.5,     # Sicherheitsfaktor für freien Speicherplatz
}