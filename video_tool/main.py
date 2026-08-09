from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

try:
    from .config import CONFIG, resolve_encoder_choice
    from .encoding import build_encoder_args, run_command
    from .media_analysis import analyze_media, has_forced_subtitles
    from .utils import ensure_dir, logger
except ImportError:  # pragma: no cover - allows direct execution from the module directory
    from config import CONFIG, resolve_encoder_choice
    from encoding import build_encoder_args, run_command
    from media_analysis import analyze_media, has_forced_subtitles
    from utils import ensure_dir, logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Modular video encoding tool")
    parser.add_argument("input_path", help="Path to the input video")
    parser.add_argument("--output-path", default=None, help="Optional output path")
    parser.add_argument("--encoder", choices=["ffmpeg", "nvencc"], default=None, help="Encoder selection")
    parser.add_argument("--codec", choices=["hevc", "av1"], default="hevc", help="Codec to use")
    parser.add_argument("--quality", type=int, default=22, help="Quality value (QVBR or CRF depending on encoder)")
    parser.add_argument("--bitrate-mode", choices=["cbr", "vbr"], default="cbr", help="Bitrate mode for ffmpeg")
    parser.add_argument("--bitrate", type=int, default=5000, help="Bitrate in kbps")
    parser.add_argument("--audio-mode", choices=["copy", "aac"], default="copy", help="Audio handling mode")
    parser.add_argument("--subtitle-burn", action="store_true", help="Burn forced subtitles")
    parser.add_argument("--ai-choice", choices=["1", "2", "3", "4"], default="1", help="AI processing mode")
    parser.add_argument("--use-nnedi", action="store_true", help="Use NNEDI for upscaling")
    parser.add_argument("--ffprobe", default=None, help="Optional explicit ffprobe path")
    return parser


def resolve_output_path(input_path: Path, output_path: Optional[str]) -> Path:
    # Falls der Nutzer explizit einen Pfad angegeben hat
    if output_path:
        out = Path(output_path).expanduser().resolve()
    else:
        # Erzeuge immer einen eindeutigen Dateinamen, egal welche Endung die Quelle hat
        out = input_path.with_name(f"{input_path.stem}_encoded.mkv")

    # Sicherheitshalber: Falls durch Zufall oder Nutzer-Eingabe Quelle == Ziel
    if out == input_path:
        out = input_path.with_name(f"{input_path.stem}_encoded_v2.mkv")

    return out


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
        logger.info("Analyzing %s", input_path)
        media_info = analyze_media(input_path, ffprobe_path=args.ffprobe)
        forced_subtitles = has_forced_subtitles(media_info.get("subtitle_streams", []))
        logger.info("Forced subtitles present: %s", forced_subtitles)

        command_args = build_encoder_args(
            input_path=input_path,
            output_path=output_path,
            encoder=selected_encoder,
            codec=args.codec,
            quality_value=args.quality,
            bitrate_mode=args.bitrate_mode,
            bitrate=args.bitrate,
            audio_mode=args.audio_mode,
            subtitle_burn=args.subtitle_burn,
            ai_choice=args.ai_choice,
            use_nnedi=args.use_nnedi,
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