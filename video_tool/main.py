from __future__ import annotations

import argparse
import logging
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from .config import WORKFLOW_DEFAULTS, recommend_quality_value, resolve_encoder_choice
    from .encoding import build_encoder_args, run_command, run_vmaf_score
    from .paths import PATHS
    from .media_analysis import (
        analyze_media,
        analyze_noise_and_quality as analyze_preflight,
        has_forced_subtitles,
    )
    from .utils import ensure_dir, logger, calculate_adjusted_speed_factor, estimate_total_duration
except ImportError:  # pragma: no cover
    from config import WORKFLOW_DEFAULTS, recommend_quality_value, resolve_encoder_choice
    from encoding import build_encoder_args, run_command, run_vmaf_score
    from paths import PATHS
    from media_analysis import (
        analyze_media,
        analyze_noise_and_quality as analyze_preflight,
        has_forced_subtitles,
    )
    from utils import ensure_dir, logger, calculate_adjusted_speed_factor, estimate_total_duration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Modular video encoding tool (Checklist UI)")
    parser.add_argument("input_path", help="Path to the input video")
    parser.add_argument("--output-path", default=None, help="Optional output path")
    parser.add_argument("--encoder", choices=["ffmpeg", "nvencc"], default=None, help="Encoder selection")
    parser.add_argument("--codec", choices=["hevc", "av1"], default=None, help="Codec to use")
    parser.add_argument("--quality", type=int, default=22, help="Base quality value")
    parser.add_argument("--skip-vmaf", action="store_true", help="Skip iterative VMAF calibration")
    parser.add_argument("--bitrate-mode", choices=["cbr", "vbr"], default="cbr", help="Bitrate mode")
    parser.add_argument("--bitrate", type=int, default=5000, help="Bitrate in kbps")
    parser.add_argument("--audio-mode", choices=["copy", "aac"], default="copy", help="Audio mode")
    parser.add_argument("--subtitle-burn", action="store_true", help="Burn forced subtitles")
    parser.add_argument("--ai-choice", choices=["1", "2", "3", "4"], default=None, help="AI processing mode")
    parser.add_argument("--use-nnedi", action="store_true", help="Use NNEDI for upscaling")
    parser.add_argument("--denoise", choices=["off", "light", "medium", "heavy"], default=None, help="Denoiser mode")
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


def copy_with_progress(src: Path, dst: Path) -> None:
    """Kopiert eine Datei mit visuellem Text-Ladebalken (simuliert Web-Upload)."""
    file_size = src.stat().st_size
    chunk_size = 1024 * 1024 * 16
    copied = 0
    bar_width = 30

    print(f"[>] Staging / Datei-Übertragung in Arbeitsverzeichnis...")
    with open(src, "rb") as f_src, open(dst, "wb") as f_dst:
        while True:
            chunk = f_src.read(chunk_size)
            if not chunk:
                break
            f_dst.write(chunk)
            copied += len(chunk)
            progress = min(copied / file_size, 1.0)
            filled = int(bar_width * progress)
            bar = "=" * filled + "-" * (bar_width - filled)
            percent = int(progress * 100)
            sys.stdout.write(f"\r    [{bar}] {percent}% ({copied // (1024*1024)} MB)")
            sys.stdout.flush()
    print()


def format_time(seconds: float) -> str:
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {mins}m {secs}s"
    return f"{mins}m {secs}s"


def get_video_duration(media_info: Dict[str, Any]) -> float:
    """Ermittelt die Gesamtdauer des Videos in Sekunden."""
    fmt = media_info.get("format", {})
    if "duration" in fmt:
        try:
            return float(fmt["duration"])
        except (ValueError, TypeError):
            pass
    v_streams = media_info.get("video_streams", [])
    if v_streams and "duration" in v_streams[0]:
        try:
            return float(v_streams[0]["duration"])
        except (ValueError, TypeError):
            pass
    return 0.0


def cleanup_workspace(work_dir: Path, staged_input_path: Optional[Path], raw_input_path: Optional[Path]) -> None:
    """Räumt temporäre Arbeitsdateien und Staging-Kopien auf."""
    print(f"[>] Bereinige temporäre Arbeitsdateien...")
    if staged_input_path and staged_input_path.exists() and raw_input_path and staged_input_path != raw_input_path:
        try:
            staged_input_path.unlink()
        except Exception as e:
            logger.debug(f"Konnte Staging-Datei nicht löschen: {e}")

    if work_dir.exists():
        for pattern in ["temp_delta_*", "*_ref_peak_*", "*_test_q*", "*_encoded.mkv", "*_uploaded.mkv"]:
            for tmp_file in work_dir.glob(pattern):
                try:
                    tmp_file.unlink()
                except Exception:
                    pass


def calibrate_quality_vmaf(
    input_path: Path,
    work_dir: Path,
    noise_plan: Dict[str, Any],
    codec: str,
    encoder: str,
    initial_q: int,
) -> Tuple[int, float]:
    """Erstellt ein Test-Sample an der Peak-Szene, kalibriert VMAF und misst die Encoding-Dauer."""
    peak_time = float(noise_plan.get("peak_timestamp_sec", 0.0))
    denoise_mode = noise_plan.get("denoise_mode", "off")

    print(f"[>] VMAF-Kalibrierung: Analysiere Peak-Szene bei {peak_time:.2f}s (Denoise: {denoise_mode})")
    logger.debug("VMAF-Kalibrierung gestartet für %s bei %.2fs mit Denoise '%s'", input_path.name, peak_time, denoise_mode)
    
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

    logger.debug("FFmpeg Referenz-Extraktionsbefehl: %s", " ".join(cut_cmd))
    print(f"    -> Erstelle 10s Referenz-Sample im Arbeitsverzeichnis...")

    try:
        run_command(cut_cmd)
    except Exception as exc:
        logger.warning("Referenz-Sample konnte nicht erstellt werden: %s. Nutze Startwert %d.", exc, initial_q)
        return initial_q, 0.0

    target_vmaf = float(noise_plan.get("target_vmaf", 95.0))
    lower_bound = float(noise_plan.get("lower_bound", target_vmaf - 0.8))
    upper_bound = float(noise_plan.get("upper_bound", target_vmaf + 0.8))

    current_q = initial_q
    best_q = current_q
    min_delta = 999.0
    last_sample_duration = 0.0

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

        logger.debug("Test-Encode Befehl (Versuch %d, Q=%d): %s", attempt, current_q, " ".join(test_cmd))
        print(f"    -> Teste Durchlauf {attempt}/3 mit Qualitätsstufe Q={current_q}...")

        try:
            t_start = time.time()
            run_command(test_cmd)
            last_sample_duration = time.time() - t_start
        except Exception as exc:
            logger.warning("Test-Encode fehlgeschlagen für Q=%d: %s", current_q, exc)
            break

        print(f"    -> Messung VMAF-Score für Q={current_q}...")
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

        print(f"    -> Ergebnis Versuch {attempt}: Q={current_q} -> VMAF: {vmaf_score:.2f} (Ziel: {lower_bound:.1f} - {upper_bound:.1f})")

        delta = abs(vmaf_score - target_vmaf)
        if delta < min_delta:
            min_delta = delta
            best_q = current_q

        if lower_bound <= vmaf_score <= upper_bound:
            print(f"[OK] VMAF-Zielwert im Toleranzbereich getroffen bei Q={current_q}!")
            return current_q, last_sample_duration

        if vmaf_score > upper_bound:
            current_q += 2
        else:
            current_q -= 2

        current_q = max(14, min(current_q, 36))

    print(f"[OK] Kalibrierung abgeschlossen. Optimaler Qualitätswert: Q={best_q}")
    return best_q, last_sample_duration

def run_encoding_job(config: dict):
    """Der neue Wrapper für das Backend."""
    # 1. Konfiguration vorbereiten (Mapping vom Pydantic Dict auf lokale Variablen)
    # Wir emulieren hier die argparse-Struktur, ohne argparse zu nutzen
    class Args:
        input_path = config.get("input_path")
        output_path = None
        encoder = config.get("encoder") # falls du es vom Frontend mitgibst
        codec = config.get("codec")
        quality = 22
        skip_vmaf = False
        bitrate_mode = "cbr"
        bitrate = 5000
        audio_mode = "copy"
        subtitle_burn = config.get("subtitle_burn")
        ai_choice = config.get("ai_choice")
        use_nnedi = False
        denoise = config.get("denoise") # "off", "light", etc.
        ffprobe = None
        ffmpeg = None

    args = Args()
    start_time = time.time()
     
    results_dir = PATHS.get("results", SCRIPT_DIR / "Results")
    work_dir = results_dir / "Work"
    ensure_dir(results_dir)
    ensure_dir(work_dir)
    
    # Logging zentral & global konfigurieren (striktes Unterdrücken von INFO/DEBUG in der Konsole)
    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    log_file_path = results_dir / f"encode_{timestamp_str}.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    # Alle Submodul-Logger bereinigen, damit sie keine eigenen Handler behalten
    for _, logger_obj in logging.root.manager.loggerDict.items():
        if isinstance(logger_obj, logging.Logger):
            logger_obj.setLevel(logging.DEBUG)
            for h in list(logger_obj.handlers):
                logger_obj.removeHandler(h)
            logger_obj.propagate = True

    # 1. Datei-Handler (schreibt alles ab DEBUG in die Log-Datei)
    file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger.addHandler(file_handler)

    # 2. Konsolen-Handler (gibt im Terminal NUR ab WARNING aus -> kein [INFO]-Lärm)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(console_handler)

    print("==================================================")
    print(" PIPELINE CHECKLIST INITIALISIERT")
    print(f" Log-Datei: {log_file_path.name}")
    print("==================================================")

    staged_input_path: Optional[Path] = None
    raw_input_path: Optional[Path] = None

    try:
        # ------------------------------------------------------------------
        # SCHRITT 1: Hardware-Check & Auswahl (mit Plattform-Erkennung)
        # ------------------------------------------------------------------
        os_name = platform.system()
        selected_encoder = resolve_encoder_choice(args.encoder)
        if selected_encoder == "nvencc":
            print(f"[OK] Hardware-Check: {os_name} / NVIDIA NVEncC aktiv (GPU Beschleunigung)")
        else:
            print(f"[OK] Hardware-Check: {os_name} / CPU-Modus erzwungen / Fallback (FFmpeg)")

        if config.get("is_staged"):
            input_path = Path(args.input_path).resolve()
            raw_input_path = input_path
            print(f"[OK] Datei aus Browser-Upload übernommen: {input_path.name}")
        else:
            raw_input_path = Path(args.input_path).expanduser().resolve()
            if not raw_input_path.exists():
                print(f"[X] FEHLER: Eingabedatei nicht gefunden: {raw_input_path}")
                return 1

            
            # ------------------------------------------------------------------
            # SCHRITT 2: Dateianzeige / Start der Verarbeitung
            # ------------------------------------------------------------------
            print(f"[>] Verarbeite Film: {raw_input_path.name} ...")

            # ------------------------------------------------------------------
            # SCHRITT 3: Dateigröße & Speicherplatz-Prüfung im Zielverzeichnis
            # ------------------------------------------------------------------
            file_size_bytes = raw_input_path.stat().st_size
            file_size_gb = file_size_bytes / (1024**3)
            
            target_disk = work_dir.anchor
            disk_usage = shutil.disk_usage(target_disk)
            free_space_gb = disk_usage.free / (1024**3)
            required_space_gb = file_size_gb * WORKFLOW_DEFAULTS.get("disk_space_multiplier", 1.5)

            if free_space_gb < required_space_gb:
                print(f"[X] FEHLER: Zu wenig Speicherplatz! Frei: {free_space_gb:.1f} GB, Benötigt: ~{required_space_gb:.1f} GB")
                return 1
            print(f"[OK] Speicherplatz-Prüfung: OK (Frei: {free_space_gb:.1f} GB | Quelldatei: {file_size_gb:.2f} GB)")

            # ------------------------------------------------------------------
            # SCHRITT 4: Staging (Upload in Arbeitsverzeichnis)
            # ------------------------------------------------------------------
            max_stem_length = 100
            safe_stem = raw_input_path.stem[:max_stem_length]

            # Prüfen, ob die Datei bereits im Work-Verzeichnis liegt (z.B. Web-Upload)
            if raw_input_path.parent.resolve() == work_dir.resolve():
                staged_input_path = raw_input_path
                print(f"[OK] Datei aus Web-Upload im Arbeitsverzeichnis erkannt: {staged_input_path.name}")
            else:
                # CLI-Modus: Externe Datei mit sicherem Namen & _uploaded ins Work-Verzeichnis kopieren
                staged_filename = f"{safe_stem}_uploaded{raw_input_path.suffix}"
                staged_input_path = work_dir / staged_filename
                
                if raw_input_path != staged_input_path:
                    copy_with_progress(raw_input_path, staged_input_path)
                else:
                    print(f"[OK] Datei liegt bereits im Arbeitsverzeichnis.")
                    
            input_path = staged_input_path

        # ------------------------------------------------------------------
        # SCHRITT 5 & 6: Medien-Analyse & Einheitliche AI-Modus-Bestimmung
        # ------------------------------------------------------------------
        print(f"[>] Analysiere Mediendatei...")
        print(f"    -> Ermittle Bitraten-Peak via FFprobe Paket-Scan (30s Fenster)...")
        
        media_info = analyze_media(input_path, ffprobe_path=args.ffprobe)
        video_duration = get_video_duration(media_info)

        v_streams = media_info.get("video_streams", [])
        source_height = int(v_streams[0]["height"]) if v_streams and v_streams[0].get("height") else 0

        # Interlaced / Progressive Check für NNEDI
        video_stream = v_streams[0] if v_streams else {}
        field_order = video_stream.get("field_order", "unknown")
        is_interlaced = field_order not in ("progressive", "unknown", "")

        if is_interlaced:
            print(f"[!] Interlaced-Material erkannt (Field Order: {field_order}) -> NNEDI/Deinterlacing-Filter erforderlich.")
        else:
            print(f"[OK] Scan-Typ: Progressive (Kein klassisches Deinterlacing nötig)")

        # Codec-Auswahl
        selected_codec = args.codec.lower() if args.codec else WORKFLOW_DEFAULTS.get("default_codec", "av1")

        # --- EINHEITLICHE AI-CHOICE LOGIK ---
        if args.ai_choice:
            ai_choice = args.ai_choice
        else:
            ai_choice = "2" if WORKFLOW_DEFAULTS.get("default_true_hdr", True) else "1"

        # Auflösungs-Mapping:
        # 1080p+ (HD)     -> Modus 1 (Standard) oder 2 (TrueHDR)
        # 480p/576p (SD)  -> Modus 3 (Upscaling ohne TrueHDR) oder 4 (Upscaling mit TrueHDR)
        if 0 < source_height <= 576:
            if ai_choice == "1":
                ai_choice = "3"
            elif ai_choice == "2":
                ai_choice = "4"
            print(f"[!] SD-Auflösung erkannt ({source_height}p) -> AI-Modus automatisch auf {ai_choice} angepasst (inkl. DVD2HD Upscaling)")
        elif source_height >= 1080:
            print(f"[OK] Auflösungs-Check: High-Definition erkannt ({source_height}p) -> AI-Modus {ai_choice}")
        else:
            print(f"[OK] Auflösungs-Check: Auflösung bei {source_height}p -> AI-Modus {ai_choice}")

        print(f"[OK] Konfiguration: Codec = {selected_codec.upper()} | AI-Modus = {ai_choice}")

        bitrate_info = media_info.get("bitrate_info", {})
        print(f"    -> Schnitt = {bitrate_info.get('avg_kbps', 0)} kbps | Peak = {bitrate_info.get('peak_kbps', 0)} kbps")

        # ------------------------------------------------------------------
        # SCHRITT 7: Preflight-Rauschanalyse & VMAF-Kalibrierung
        # ------------------------------------------------------------------
        print(f"[>] Starte Preflight-Rauschanalyse (Kompressions-Delta)...")
        
        noise_plan = analyze_preflight(
            file_path=input_path,
            bitrate_info=bitrate_info,
            ffprobe_path=args.ffprobe,
            ffmpeg_path=args.ffmpeg,
            work_dir=work_dir,
            encoder=selected_encoder,
            codec=selected_codec,
        )

        print(f"    -> Rauschlevel: {noise_plan.get('noise_level')} | Denoise: {noise_plan.get('denoise_mode')} | Target VMAF: {noise_plan.get('target_vmaf')}")

        initial_quality = recommend_quality_value(
            plan=noise_plan,
            codec=selected_codec,
            encoder=selected_encoder,
            requested_quality=args.quality,
        )

        sample_duration = 0.0
        if not args.skip_vmaf:
            final_quality, sample_duration = calibrate_quality_vmaf(
                input_path=input_path,
                work_dir=work_dir,
                noise_plan=noise_plan,
                codec=selected_codec,
                encoder=selected_encoder,
                initial_q=initial_quality,
            )
        else:
            final_quality = initial_quality
            print(f"[OK] VMAF-Kalibrierung übersprungen. Verwende Q={final_quality}")

        print(f"[OK] Finaler Qualitätswert festgelegt: Q={final_quality}")

        if sample_duration > 0 and video_duration > 0:
            adjusted_factor = calculate_adjusted_speed_factor(10/sample_duration, ai_choice)
            estimated_total_seconds = estimate_total_duration(video_duration, adjusted_factor)
            print(f"[OK] Geschätzte Encoding-Dauer: ca. {format_time(estimated_total_seconds)}")
        else:
            print(f"[OK] Geschätzte Encoding-Dauer: (Nicht verfügbar)")

        # ------------------------------------------------------------------
        # SCHRITT 8: Haupt-Encode mit ETA & Fortschritt (in Work schreiben)
        # ------------------------------------------------------------------
        # Während des Encodens IMMER im Work-Verzeichnis arbeiten
        clean_stem = raw_input_path.stem
        if clean_stem.endswith("_uploaded"):
            clean_stem = clean_stem[:-9] # Schneidet "_uploaded" (9 Zeichen) ab
        temp_encoded_path = work_dir / f"{clean_stem}_encoded.mkv"
        clean_raw_path = raw_input_path.with_name(f"{clean_stem}{raw_input_path.suffix}")
        final_output_path = resolve_output_path(clean_raw_path, args.output_path)

        print(f"[>] Starte Haupt-Encode (im Arbeitsverzeichnis)...")
        command_args = build_encoder_args(
            input_path=input_path,
            output_path=temp_encoded_path,
            encoder=selected_encoder,
            codec=selected_codec,
            quality_value=final_quality,
            bitrate_mode=args.bitrate_mode,
            bitrate=args.bitrate,
            audio_mode=args.audio_mode,
            subtitle_burn=args.subtitle_burn or has_forced_subtitles(media_info.get("subtitle_streams", [])),
            ai_choice=ai_choice,
            use_nnedi=args.use_nnedi or is_interlaced or ai_choice in {"3", "4"},
            quality_metric="none",
            denoise_mode=args.denoise or noise_plan.get("denoise_mode", "off"),
            extra_args=noise_plan.get("extra_args", []),
        )

        logger.debug("Haupt-Encode Befehl: %s", " ".join(command_args))
        run_command(command_args)

        # Nach erfolgreichem Encode die fertige Datei eine Ebene höher in den Zielordner schieben
        if temp_encoded_path.exists():
            if temp_encoded_path != final_output_path:
                print(f"[>] Verschiebe fertige Zieldatei nach Results...")
                if final_output_path.exists():
                    final_output_path.unlink()
                shutil.move(str(temp_encoded_path), str(final_output_path))

        # ------------------------------------------------------------------
        # SCHRITT 9: Bereinigung & Abschluss
        # ------------------------------------------------------------------
        cleanup_workspace(work_dir, staged_input_path, raw_input_path)

        total_duration = time.time() - start_time
        formatted_duration = format_time(total_duration)
        
        # Einheitliche Abschlussnachricht definieren
        completion_msg = (
            "==================================================\n"
            "[OK] FERTIG! Encoding erfolgreich abgeschlossen.\n"
            f"    Zieldatei: {final_output_path}\n"
            f"    Gesamtlaufzeit: {formatted_duration}\n"
            f"    Protokoll gespeichert unter: {log_file_path}\n"
            "=================================================="
        )

        # 1. Ins Terminal schreiben
        print(completion_msg + "\n")
        
        # 2. In die Log-Datei schreiben (damit es für die Analyse erhalten bleibt)
        logging.info(completion_msg)
        print("[UI_RESET]")
        return 0

    
    except KeyboardInterrupt:
        print("\n\n[!] ABBRUCH: Durch Benutzer unterbrochen (Strg+C).")
        logger.warning("Pipeline durch Benutzer via Strg+C abgebrochen.")
        cleanup_workspace(work_dir, staged_input_path, raw_input_path)
        print(f"[OK] Arbeitsverzeichnis erfolgreich bereinigt.")
        print(f"[OK] Protokoll wurde gesichert unter: {log_file_path}")
        return 1

    except Exception as exc:
        logger.exception("Fehler während der Ausführung: %s", exc)
        print(f"\n[X] KRITISCHER FEHLER: {exc}")
        cleanup_workspace(work_dir, staged_input_path, raw_input_path)
        print(f"[OK] Arbeitsverzeichnis erfolgreich bereinigt.")
        print(f"[OK] Protokoll wurde gesichert unter: {log_file_path}")
        return 1

    finally:
        # WICHTIG: Schließt den Dateizugriff, damit die Log-Datei freigegeben wird!
        file_handler.close()
        root_logger.removeHandler(file_handler)
        if console_handler in root_logger.handlers:
            root_logger.removeHandler(console_handler)
    
def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    return run_encoding_job(vars(args))


if __name__ == "__main__":
    sys.exit(main())