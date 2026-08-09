from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Optional

try:
    from .config import resolve_encoder_choice
    from .encoding import build_encoder_args, run_command
    from .media_analysis import analyze_media, analyze_noise_and_quality as analyze_preflight, has_forced_subtitles, recommend_quality_value
    from .utils import ensure_dir, logger
except ImportError:  # pragma: no cover - allows direct execution from the module directory
    from config import resolve_encoder_choice
    from encoding import build_encoder_args, run_command
    from media_analysis import analyze_media, analyze_noise_and_quality as analyze_preflight, has_forced_subtitles, recommend_quality_value
    from utils import ensure_dir, logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Modular video encoding tool")
    parser.add_argument("input_path", help="Path to the input video")
    parser.add_argument("--output-path", default=None, help="Optional output path")
    parser.add_argument("--encoder", choices=["ffmpeg", "nvencc"], default=None, help="Encoder selection")
    parser.add_argument("--codec", choices=["hevc", "av1"], default=None, help="Codec to use")
    parser.add_argument("--quality", type=int, default=22, help="Quality value (QVBR or CRF depending on encoder)")
    parser.add_argument("--bitrate-mode", choices=["cbr", "vbr"], default="cbr", help="Bitrate mode for ffmpeg")
    parser.add_argument("--bitrate", type=int, default=5000, help="Bitrate in kbps")
    parser.add_argument("--audio-mode", choices=["copy", "aac"], default="copy", help="Audio handling mode")
    parser.add_argument("--subtitle-burn", action="store_true", help="Burn forced subtitles")
    parser.add_argument("--ai-choice", choices=["1", "2", "3", "4"], default=None, help="AI processing mode")
    parser.add_argument("--use-nnedi", action="store_true", help="Use NNEDI for upscaling")
    parser.add_argument("--denoise", choices=["off", "light", "medium", "heavy"], default=None, help="Denoiser mode")
    parser.add_argument("--grain", choices=["off", "light", "medium", "heavy"], default=None, help="Grain handling mode")
    parser.add_argument("--ffprobe", default=None, help="Optional explicit ffprobe path")
    return parser


def resolve_output_path(input_path: Path, output_path: Optional[str]) -> Path:
    if output_path:
        out = Path(output_path).expanduser().resolve()
    else:
        out = input_path.with_name(f"{input_path.stem}_encoded.mkv")

    if out == input_path:
        out = input_path.with_name(f"{input_path.stem}_encoded_v2.mkv")

    return out


def get_default_ai_mode(source_height: Optional[int]) -> str:
    if source_height is None:
        return "1"
    if source_height <= 576:
        return "4"
    if source_height >= 1080:
        return "2"
    return "1"


def choose_codec(cli_codec: Optional[str]) -> str:
    if cli_codec:
        return cli_codec

    if not sys.stdin.isatty():
        return "av1"

    print("Codec-Auswahl (10 Sekunden Timeout): [1] AV1 [2] HEVC")
    print("Standard: AV1")

    result = {"value": None}

    def _read_input() -> None:
        try:
            raw = input().strip().lower()
        except Exception:
            raw = ""
        if raw in {"1", "av1"}:
            result["value"] = "av1"
        elif raw in {"2", "hevc"}:
            result["value"] = "hevc"
        elif raw == "":
            result["value"] = "av1"
        else:
            result["value"] = "av1"

    thread = threading.Thread(target=_read_input, daemon=True)
    thread.start()
    thread.join(10.0)

    return result["value"] or "av1"


def choose_ai_mode(cli_choice: Optional[str], default_mode: str) -> str:
    if cli_choice:
        return cli_choice

    if not sys.stdin.isatty():
        return default_mode

    labels = {
        "1": "Standard (Keine AI-Filter)",
        "2": "SDR2HDR (HDR-Enhancement)",
        "3": "DVD2HD (AI-Upscaling)",
        "4": "DVD2HD + TrueHDR (Upscaling + HDR)",
    }

    print("AI-Verarbeitungsmodus (10 Sekunden Timeout):")
    for key, label in labels.items():
        print(f"  [{key}] {label}")
    print(f"Standard: [{default_mode}] {labels[default_mode]}")

    result = {"value": None}

    def _read_input() -> None:
        try:
            raw = input().strip()
        except Exception:
            raw = ""
        if raw in labels:
            result["value"] = raw
        elif raw == "":
            result["value"] = default_mode
        elif raw in {"av1", "hevc"}:
            result["value"] = "1"
        else:
            result["value"] = default_mode

    thread = threading.Thread(target=_read_input, daemon=True)
    thread.start()
    thread.join(10.0)

    return result["value"] or default_mode


def analyze_noise_and_quality(input_path: Path, ffprobe_path: Optional[str]) -> dict:
    try:
        return analyze_preflight(file_path=input_path, ffprobe_path=ffprobe_path)
    except Exception as exc:
        logger.warning("Could not run noise/VMAF preflight analysis: %s", exc)
        return {
            "denoise_mode": "off",
            "grain_mode": "off",
            "extra_args": [],
            "target_vmaf": 97.0,
            "lower_bound": 96.5,
            "upper_bound": 97.5,
            "noise_detected": False,
            "noise_level": "none",
        }


def detect_source_height(input_path: Path, ffprobe_path: Optional[str]) -> Optional[int]:
    try:
        media_info = analyze_media(input_path, ffprobe_path=ffprobe_path)
        streams = media_info.get("video_streams") or []
        if not streams:
            return None
        height = streams[0].get("height")
        if height is None:
            return None
        return int(height)
    except Exception as exc:
        logger.warning("Could not analyze source resolution: %s", exc)
        return None


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        input_path = Path(args.input_path).expanduser().resolve()
        if not input_path.exists():
            logger.error("Input file does not exist: %s", input_path)
            return 1

        output_path = resolve_output_path(input_path, args.output_path)
        ensure_dir(output_path.parent)

        selected_encoder = resolve_encoder_choice(args.encoder)
        source_height = detect_source_height(input_path, args.ffprobe)
        default_ai_mode = get_default_ai_mode(source_height)
        noise_plan = analyze_noise_and_quality(input_path, args.ffprobe)

        selected_codec = choose_codec(args.codec)
        selected_ai_mode = choose_ai_mode(args.ai_choice, default_ai_mode)

        logger.info("Selected codec: %s", selected_codec)
        logger.info("Selected AI mode: %s", selected_ai_mode)
        logger.info("Noise/VMAF preflight: level=%s, denoise=%s, target_vmaf=%.1f", noise_plan["noise_level"], noise_plan["denoise_mode"], noise_plan["target_vmaf"])
        logger.info("Analyzing %s", input_path)
        media_info = analyze_media(input_path, ffprobe_path=args.ffprobe)
        forced_subtitles = has_forced_subtitles(media_info.get("subtitle_streams", []))
        logger.info("Forced subtitles present: %s", forced_subtitles)

        recommended_quality = recommend_quality_value(
            noise_plan,
            codec=selected_codec,
            encoder=selected_encoder,
            requested_quality=args.quality,
        )
        logger.info("Recommended quality value: %s", recommended_quality)

        command_args = build_encoder_args(
            input_path=input_path,
            output_path=output_path,
            encoder=selected_encoder,
            codec=selected_codec,
            quality_value=recommended_quality,
            bitrate_mode=args.bitrate_mode,
            bitrate=args.bitrate,
            audio_mode=args.audio_mode,
            subtitle_burn=args.subtitle_burn,
            ai_choice=selected_ai_mode,
            use_nnedi=args.use_nnedi or selected_ai_mode in {"3", "4"},
            quality_metric="vmaf",
            denoise_mode=args.denoise or noise_plan["denoise_mode"],
            grain_mode=args.grain or noise_plan["grain_mode"],
            extra_args=noise_plan.get("extra_args", []),
        )

        logger.info("Prepared command: %s", " ".join(str(item) for item in command_args))
        run_command(command_args)
        logger.info("Encoding completed successfully: %s", output_path)
        return 0
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return 1
    except FileNotFoundError as exc:
        logger.error("File or executable error: %s", exc)
        return 1
    except RuntimeError as exc:
        logger.error("Encoding failed: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.exception("Unexpected exception: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
