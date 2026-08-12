from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from .config import PATHS, recommend_quality_value, resolve_encoder_choice
    from .encoding import build_encoder_args, run_command, run_vmaf_score
    from .media_analysis import (
        analyze_media,
        analyze_noise_and_quality as analyze_preflight,
        has_forced_subtitles,
    )
    from .utils import ensure_dir, logger
except ImportError:  # pragma: no cover
    from config import PATHS, recommend_quality_value, resolve_encoder_choice
    from encoding import build_encoder_args, run_command, run_vmaf_score
    from media_analysis import (
        analyze_media,
        analyze_noise_and_quality as analyze_preflight,
        has_forced_subtitles,
    )
    from utils import ensure_dir, logger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Modular video encoding tool")
    parser.add_argument("input_path", help="Path to the input video")
    parser.add_argument("--output-path", default=None, help="Optional output path")
    parser.add_argument("--encoder", choices=["ffmpeg", "nvencc"], default=None, help="Encoder selection")
    parser.add_argument("--codec", choices=["hevc", "av1"], default=None, help="Codec to use")
    parser.add_argument("--quality", type=int, default=22, help="Base quality value (QVBR or CRF)")
    parser.add_argument("--skip-vmaf", action="store_true", help="Skip iterative VMAF calibration run")
    parser.add_argument("--bitrate-mode", choices=["cbr", "vbr"], default="cbr", help="Bitrate mode for ffmpeg")
    parser.add_argument("--bitrate", type=int, default=5000, help="Bitrate in kbps")
    parser.add_argument("--audio-mode", choices=["copy", "aac"], default="copy", help="Audio handling mode")
    parser.add_argument("--subtitle-burn", action="store_true", help="Burn forced subtitles")
    parser.add_argument("--ai-choice", choices=["1", "2", "3", "4"], default=None, help="AI processing mode")
    parser.add_argument("--use-nnedi", action="store_true", help="Use NNEDI for upscaling")
    parser.add_argument("--denoise", choices=["off", "light", "medium", "heavy"], default=None, help="Denoiser mode")
    parser.add_argument("--grain", choices=["off", "light", "medium", "heavy"], default=None, help="Grain handling mode")
    parser.add_argument("--ffprobe", default=None, help="Optional explicit ffprobe path")
    parser.add_argument("--ffmpeg", default=None, help="Optional explicit ffmpeg path")
    return parser


def resolve_output_path(input_path: Path, output_path: Optional[str]) -> Path:
    target = Path(output_path).expanduser().resolve() if output_path else PATHS["results"]
    if target.is_dir() or target.suffix == "":
        ensure_dir(target)
        out = target / f"{input_path.stem}_encoded.mkv"
    else:
        ensure_dir(target.parent)
        out = target

    if out == input_path:
        out = out.with_name(f"{input_path.stem}_encoded_v2.mkv")

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
        return cli_codec.lower()

    if not sys.stdin.isatty():
        return "av1"

    print("\nCodec-Auswahl (10 Sekunden Timeout):")
    print("1) HEVC")
    print("2) AV1 <-- Default")

    result = {"value": None}

    def _read_input() -> None:
        try:
            raw = input().strip().lower()
        except Exception:
            raw = ""
        if raw in {"1", "hevc"}:
            result["value"] = "hevc"
        elif raw in {"2", "av1", ""}:
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

    print("\nAI-Verarbeitungsmodus (10 Sekunden Timeout):")
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
        else:
            result["value"] = default_mode

    thread = threading.Thread(target=_read_input, daemon=True)
    thread.start()
    thread.join(10.0)

    return result["value"] or default_mode


def calibrate_quality_vmaf(
    input_path: Path,
    work_dir: Path,
    noise_plan: Dict[str, Any],
    codec: str,
    encoder: str,
    initial_q: int,
) -> int:
    """Erstellt ein Test-Sample an der Peak-Szene und kalibriert VMAF (mit Denoise, aber OHNE AI/HDR)."""
    peak_time = float(noise_plan.get("peak_timestamp_sec", 0.0))
    denoise_mode = noise_plan.get("denoise_mode", "off")

    logger.info(
        "Starte VMAF-Kalibrierung an Peak-Szene (%.2fs) mit Denoise-Mode '%s' (AI/HDR deaktiviert)...",
        peak_time, denoise_mode
    )
    ensure_dir(work_dir)

    ffmpeg_bin = PATHS.get("ffmpeg", Path("ffmpeg"))
    ref_sample = work_dir / f"{input_path.stem}_ref_peak_10s.mkv"

    cut_cmd = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", str(peak_time),
        "-i", str(input_path),
        "-t", "10",
        "-map", "0:v:0",
        "-c:v", "copy",
        "-avoid_negative_ts", "make_zero",
        str(ref_sample),
    ]

    try:
        run_command(cut_cmd)
    except Exception as exc:
        logger.warning("Referenz-Sample konnte nicht erstellt werden: %s. Nutze Startwert %d.", exc, initial_q)
        return initial_q

    target_vmaf = float(noise_plan.get("target_vmaf", 95.0))
    lower_bound = float(noise_plan.get("lower_bound", target_vmaf - 0.8))
    upper_bound = float(noise_plan.get("upper_bound", target_vmaf + 0.8))

    current_q = initial_q
    best_q = current_q
    min_delta = 999.0

    for attempt in range(1, 4):
        test_encoded = work_dir / f"{input_path.stem}_test_q{current_q}.mkv"

        test_cmd = build_encoder_args(
            input_path=ref_sample,
            output_path=test_encoded,
            encoder=encoder,
            codec=codec,
            quality_value=current_q,
            ai_choice="1",
            use_nnedi=False,
            denoise_mode=denoise_mode,
            extra_args=noise_plan.get("extra_args", []),
        )

        try:
            run_command(test_cmd)
        except Exception as exc:
            logger.warning("Test-Encode fehlgeschlagen für Q=%d: %s", current_q, exc)
            break

        vmaf_score = run_vmaf_score(
            reference_path=ref_sample,
            encoded_sample_path=test_encoded,
            sample_start=0,
            sample_duration=10,
            ffmpeg_bin=Path(ffmpeg_bin),
        )

        if vmaf_score <= 0.0:
            logger.warning("VMAF-Messung liefert kein Ergebnis. Breche Kalibrierung ab.")
            break

        logger.info(
            "VMAF-Messung am Peak (Versuch %d): Q=%d -> VMAF: %.2f (Ziel: %.1f - %.1f)",
            attempt, current_q, vmaf_score, lower_bound, upper_bound
        )

        delta = abs(vmaf_score - target_vmaf)
        if delta < min_delta:
            min_delta = delta
            best_q = current_q

        if lower_bound <= vmaf_score <= upper_bound:
            logger.info("Ziel-VMAF erreicht: Q=%d mit VMAF=%.2f", current_q, vmaf_score)
            return current_q

        if vmaf_score > upper_bound:
            current_q += 2
        else:
            current_q -= 2

        current_q = max(14, min(current_q, 36))

    logger.info("VMAF-Kalibrierung abgeschlossen. Bester Wert: Q=%d", best_q)
    return best_q


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        input_path = Path(args.input_path).expanduser().resolve()
        if not input_path.exists():
            logger.error("Input file does not exist: %s", input_path)
            return 1

        logger.info("==================================================")
        logger.info("Datei erfolgreich übergeben: %s", input_path.name)
        logger.info("==================================================")

        results_dir = PATHS.get("results", SCRIPT_DIR / "Results")
        work_dir = results_dir / "Work"
        ensure_dir(results_dir)
        ensure_dir(work_dir)

        output_path = resolve_output_path(input_path, args.output_path)
        selected_encoder = resolve_encoder_choice(args.encoder)

        # --- 1. Bitraten & Medienanalyse ---
        logger.info("[1/4] Starte Medien- und Bitraten-Analyse...")
        media_info = analyze_media(input_path, ffprobe_path=args.ffprobe)
        bitrate_info = media_info.get("bitrate_info", {})

        v_streams = media_info.get("video_streams", [])
        source_height = int(v_streams[0]["height"]) if v_streams and v_streams[0].get("height") else None
        default_ai_mode = get_default_ai_mode(source_height)

        logger.info(
            "Bitraten-Ergebnis: Schnitt = %d kbps | Peak = %d kbps (bei Position %.2fs)",
            bitrate_info.get("avg_kbps", 0),
            bitrate_info.get("peak_kbps", 0),
            bitrate_info.get("peak_timestamp_sec", 0.0),
        )

        selected_codec = choose_codec(args.codec)
        selected_ai_mode = choose_ai_mode(args.ai_choice, default_mode=default_ai_mode)

        # --- 2. Kompressions-Delta Rauschanalyse ---
        logger.info("[2/4] Starte gewichtete Kompressions-Delta-Rauschanalyse (33%%, 66%%, Peak)...")
        noise_plan = analyze_preflight(
            file_path=input_path,
            bitrate_info=bitrate_info,
            ffprobe_path=args.ffprobe,
            ffmpeg_path=args.ffmpeg,
            work_dir=work_dir,
            encoder=selected_encoder,
            codec=selected_codec,
        )

        logger.info("Selected encoder: %s", selected_encoder)
        logger.info("Selected codec: %s", selected_codec)
        logger.info("Selected AI mode: %s", selected_ai_mode)
        logger.info(
            "Noise preflight result: level=%s, denoise=%s, target_vmaf=%.1f (Delta weighted: %.1f%%)",
            noise_plan.get("noise_level"),
            noise_plan.get("denoise_mode"),
            noise_plan.get("target_vmaf", 96.5),
            noise_plan.get("delta_weighted", 0.0),
        )

        forced_subtitles = has_forced_subtitles(media_info.get("subtitle_streams", []))
        logger.info("Forced subtitles present: %s", forced_subtitles)

        # --- 3. VMAF-Kalibrierung ---
        initial_quality = recommend_quality_value(
            noise_plan,
            codec=selected_codec,
            encoder=selected_encoder,
            requested_quality=args.quality,
        )

        if not args.skip_vmaf:
            logger.info("[3/4] Starte VMAF-Kalibrierung am Bitraten-Peak...")
            final_quality = calibrate_quality_vmaf(
                input_path=input_path,
                work_dir=work_dir,
                noise_plan=noise_plan,
                codec=selected_codec,
                encoder=selected_encoder,
                initial_q=initial_quality,
            )
        else:
            final_quality = initial_quality
            logger.info("[3/4] VMAF-Kalibrierung übersprungen. Verwende Q=%d", final_quality)

        logger.info("Finaler Qualitätswert für Main-Encode: Q=%d", final_quality)

        # --- 4. Main-Encode ---
        logger.info("[4/4] Starte Haupt-Encode mit gewählten AI-Einstellungen...")
        command_args = build_encoder_args(
            input_path=input_path,
            output_path=output_path,
            encoder=selected_encoder,
            codec=selected_codec,
            quality_value=final_quality,
            bitrate_mode=args.bitrate_mode,
            bitrate=args.bitrate,
            audio_mode=args.audio_mode,
            subtitle_burn=args.subtitle_burn or forced_subtitles,
            ai_choice=selected_ai_mode,
            use_nnedi=args.use_nnedi or selected_ai_mode in {"3", "4"},
            quality_metric="none",
            denoise_mode=args.denoise or noise_plan.get("denoise_mode", "off"),
            extra_args=noise_plan.get("extra_args", []),
        )

        run_command(command_args)
        logger.info("Encoding erfolgreich abgeschlossen: %s", output_path)
        return 0

    except Exception as exc:
        logger.exception("Fehler während der Ausführung: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())