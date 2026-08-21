# src/pipeline.py
from __future__ import annotations
from typing import Optional
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
import re
from pathlib import Path

try:
    from .config import CONFIG, IS_WINDOWS, resolve_encoder_choice
    from .media_analysis import analyze_media, analyze_noise_and_quality, get_forced_subtitle_track, get_forced_subtitle_track_by_packet_count, calibrate_quality_vmaf, recommend_quality_value, get_video_duration, cleanup_vmaf_samples, FFPROBE_BIN
    from .encoding import build_encoder_args, run_command
    from .utils import calculate_adjusted_speed_factor, estimate_total_duration, is_ffmpeg_hardware_encoder_available
except ImportError:  # pragma: no cover
    from media_analysis import analyze_media, analyze_noise_and_quality, get_forced_subtitle_track, get_forced_subtitle_track_by_packet_count, calibrate_quality_vmaf, recommend_quality_value, get_video_duration, cleanup_vmaf_samples, FFPROBE_BIN
    from encoding import build_encoder_args, run_command
    from config import CONFIG, IS_WINDOWS, resolve_encoder_choice
    from utils import calculate_adjusted_speed_factor, estimate_total_duration, is_ffmpeg_hardware_encoder_available

WORK_DIR = Path(CONFIG["work_dir"])
RESULT_DIR = Path(CONFIG["result_dir"])
DONE_DIR = Path(CONFIG["done_dir"])
INBOX_DIR = Path(CONFIG["inbox_dir"])


def _copy_stable_input(source: Path, destination: Path) -> None:
    """Kopiert eine fertige Datei per Robocopy/Rsync und überwacht den Fortschritt."""
    stable_size = None
    stable_checks = 0
    while stable_checks < 5:
        current_size = source.stat().st_size
        if current_size > 0 and current_size == stable_size:
            stable_checks += 1
        else:
            stable_size = current_size
            stable_checks = 0
        print(
            f"[...] Warte auf stabile Quelle {source.name}: "
            f"{current_size / 1024 / 1024:.0f} MiB ({stable_checks}/5 stabil)"
        )
        if stable_checks < 5:
            time.sleep(5)

    # Destination NICHT löschen: robocopy /Z bzw. rsync --partial --inplace
    # sollen an einer bereits vorhandenen Teil-Kopie fortsetzen können.
    destination.parent.mkdir(parents=True, exist_ok=True)

    if IS_WINDOWS:
        command = [
            "robocopy",
            str(source.parent),
            str(destination.parent),
            source.name,
            # /IS: auch Dateien mit gleicher Größe/Zeitstempel erneut kopieren,
            # statt einen zufällig gleich großen Rest als "fertig" zu akzeptieren.
            "/J", "/Z", "/IS", "/R:2", "/W:5", "/NFL", "/NDL", "/NP",
        ]
    else:
        if shutil.which("rsync") is None:
            raise FileNotFoundError("rsync wurde nicht gefunden")
        command = [
            # --checksum: Inhalt statt nur Größe/mtime vergleichen, damit ein zufällig
            # gleich großer Rest in Work nicht fälschlich als vollständig gilt.
            "rsync", "--archive", "--partial", "--inplace", "--checksum",
            str(source), str(destination),
        ]

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        copy_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        source_changed = False
        while copy_process.poll() is None:
            time.sleep(5)
            current_source_size = source.stat().st_size
            current_destination_size = destination.stat().st_size if destination.exists() else 0
            if current_destination_size >= current_source_size > 0:
                # Größe schon identisch: rsync/robocopy prüfen jetzt den Inhalt
                # (--checksum bzw. /IS), das kann bei großen Dateien dauern.
                print(f"[...] Prüfe Inhalt von {source.name} (Checksum-Vergleich läuft)...")
            else:
                print(
                    f"[...] Kopiere {source.name}: "
                    f"{current_destination_size / 1024 / 1024:.0f} / "
                    f"{current_source_size / 1024 / 1024:.0f} MiB"
                )
            if current_source_size != stable_size:
                source_changed = True
                copy_process.terminate()
                break

        return_code = copy_process.wait()

        if source_changed:
            # Quelle ist während des Kopierens weitergewachsen: robocopy /Z bzw.
            # rsync --partial --inplace können ab der vorhandenen Teil-Kopie fortsetzen,
            # statt komplett neu zu beginnen.
            stable_size = source.stat().st_size
            print(f"[...] Quelle hat sich verändert, setze Kopiervorgang fort (Versuch {attempt}/{max_attempts})...")
            continue

        if (IS_WINDOWS and return_code >= 8) or (not IS_WINDOWS and return_code != 0):
            destination.unlink(missing_ok=True)
            raise IOError(f"Kopieren nach Work fehlgeschlagen (Exit-Code {return_code}): {source.name}")

        source_size = source.stat().st_size
        destination_size = destination.stat().st_size
        if source_size != stable_size or destination_size != source_size:
            stable_size = source_size
            print(f"[...] Quelle hat sich nach Kopierende noch verändert, setze fort (Versuch {attempt}/{max_attempts})...")
            continue

        return

    destination.unlink(missing_ok=True)
    raise IOError(f"Quelle wurde während des Kopierens wiederholt verändert: {source.name}")


def _output_encoder_label(encoder: str) -> str:
    return {
        "nvencc": "NV",
        "nvencc64": "NV",
        "nvenc": "NV",
        "qsv": "QSV",
        "vcenc": "VCENC",
        "vceenc": "VCENC",
        "ffmpeg": "FFMPEG",
    }.get(encoder.lower().strip(), encoder.upper().strip())


def _output_codec_label(codec: str) -> str:
    return {
        "hevc": "HEVC",
        "h265": "HEVC",
        "av1": "AV1",
        "h264": "H264",
    }.get(codec.lower().strip(), codec.upper().strip())


def process_job(
    input_path: Path,
    codec: Optional[str] = None,
    encoder: Optional[str] = None,
    quality: int = 22,
    ai_mode: Optional[str] = None,
) -> None:
    encoder = resolve_encoder_choice(encoder)
    requested_encoder = encoder
    codec = codec or str(CONFIG.get("default_codec", "av1"))
    if encoder in ("qsv", "vcenc"):
        if not is_ffmpeg_hardware_encoder_available(encoder, codec):
            logger = logging.getLogger("omni_pipeline")
            logger.warning("Encoder '%s' für Codec '%s' nicht verfügbar. Fallback auf ffmpeg.", encoder, codec)
            encoder = "ffmpeg"
    elif encoder in ("nvencc", "nvencc64"):
        configured_nvencc = str(CONFIG.get("tools", {}).get("nvencc", "nvencc"))
        nvencc_path = Path(configured_nvencc)
        if not nvencc_path.exists() and shutil.which(configured_nvencc) is None:
            logger = logging.getLogger("omni_pipeline")
            logger.warning("NVEncC '%s' nicht verfügbar. Fallback auf ffmpeg.", configured_nvencc)
            encoder = "ffmpeg"
    if requested_encoder != encoder:
        logging.getLogger("omni_pipeline").info("Verwende Encoder: %s", encoder)
    # 1. Eindeutige Job-ID erzeugen
    clean_stem = re.sub(r'[^\w\-_.]', '_', input_path.stem).strip('_')
    job_id = f"{clean_stem}_{uuid.uuid4().hex[:6]}"

    # Pfade definieren: filmname_a1b2c3.job.json & filmname_a1b2c3.log
    job_json_path = WORK_DIR / f"{job_id}.job.json"
    job_log_path = WORK_DIR / f"{job_id}.log"
    work_input = WORK_DIR / input_path.name
    output_stem = input_path.stem
    output_suffix = f"_{_output_encoder_label(encoder)}_{_output_codec_label(codec)}"
    work_output = WORK_DIR / f"{output_stem}{output_suffix}.mkv"

    # Prüfen, ob die Datei aus der INBOX kommt
    is_from_inbox = (INBOX_DIR.resolve() in input_path.resolve().parents) or (input_path.parent.resolve() == INBOX_DIR.resolve())

    if is_from_inbox:
        # Kommt aus INBOX: Lokal nach Work kopieren, damit die Verarbeitung nicht über SMB läuft.
        if input_path.resolve() != work_input.resolve():
            _copy_stable_input(input_path, work_input)
    else:
        # Kommt von der CLI / einem externen Pfad: Kopieren nach Work, damit Original erhalten bleibt
        if input_path.resolve() != work_input.resolve():
            shutil.copy2(str(input_path), str(work_input))

    print(f"[OK] Kopie abgeschlossen: {work_input.name}. Starte jetzt Analyse und Transcoding...")

    # 2. Job-spezifischen Logger aufsetzen
    logger = logging.getLogger(f"omni.{job_id}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # Vorherige Handler säubern

    # File-Handler: Schreibt genau in Work/{job_id}.log
    file_handler = logging.FileHandler(job_log_path, encoding="utf-8")
    file_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Konsole weiterhin mitversorgen
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(file_formatter)
    logger.addHandler(console_handler)

    # Manifest-Helfer
    job_data = {
        "job_id": job_id,
        "input_file": str(work_input),
        "status": "processing",
        "step": "INIT",
        "options": {"codec": codec, "encoder": encoder, "quality": quality}
    }

    def save_manifest(data: dict) -> None:
        with open(job_json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

    save_manifest(job_data)
    logger.info(f"=== Job {job_id} gestartet für {work_input.name} ===")
    start_time = time.time()

    try:
        # SCHRITT 1: Analyse
        job_data["step"] = "ANALYZING"
        save_manifest(job_data)
        configured_ai_mode = str(ai_mode or CONFIG.get("default_ai_choice", "2"))
        media_info = analyze_media(work_input, ai_mode=configured_ai_mode)
        ai_mode = media_info.get("ai_mode", configured_ai_mode)
        subtitle_streams = media_info.get("subtitle_streams", [])
        forced_subtitle_track = get_forced_subtitle_track(subtitle_streams)
        subtitle_forced_only = (
            forced_subtitle_track is not None
            and str(subtitle_streams[forced_subtitle_track].get("disposition", {}).get("forced", 0)) == "1"
        )
        if forced_subtitle_track is None:
            # Fallback: keine Disposition/Titel-Markierung vorhanden, aber evtl.
            # eine auffällig kleine Spur (nur Signs/Fremdsprachen-Cues statt Volldialog).
            forced_subtitle_track = get_forced_subtitle_track_by_packet_count(work_input, subtitle_streams)
            if forced_subtitle_track is not None:
                logger.info("Forced Sub per Paketanzahl-Heuristik erkannt: Spur %d", forced_subtitle_track)
        forced_subs = forced_subtitle_track is not None
        forced_subtitle_codec = (
            subtitle_streams[forced_subtitle_track].get("codec_name")
            if forced_subtitle_track is not None
            else None
        )
        is_interlaced = media_info.get("is_interlaced", False)

        # SCHRITT 2: Preflight & VMAF
        job_data["step"] = "NOISE_ANALYSIS"
        save_manifest(job_data)

        preflight_plan = analyze_noise_and_quality(
            file_path=work_input,
            work_dir=WORK_DIR,
            encoder=encoder,
            codec=codec,
            logger=logger
        )

        logger.info(f"    -> Rauschlevel: {preflight_plan.get('noise_level')} | Denoise: {preflight_plan.get('denoise_mode')} | Target VMAF: {preflight_plan.get('target_vmaf')}")

        optimal_cq = recommend_quality_value(
            plan=preflight_plan,
            codec=codec,
            encoder=encoder,
            requested_quality=quality
        )

        job_data["step"] = "VMAF_ANALYSIS"
        save_manifest(job_data)

        sample_duration = 0.0
        final_quality, sample_duration = calibrate_quality_vmaf(
                input_path=work_input,
                work_dir=WORK_DIR,
                noise_plan=preflight_plan,
                codec=codec,
                encoder=encoder,
                initial_q=optimal_cq,
                is_interlaced=is_interlaced,
            )
        
        logger.info(f"VMAF-Analyse beendet. Optimaler CQ/CRF-Wert: {final_quality}. Interlaced: {is_interlaced}.")
        job_data["computed_cq"] = final_quality

        # SCHRITT 3: Transkodierung
        job_data["step"] = "ENCODING"
        logger.info(f"Starte Haupt-Encoding ({codec.upper()} via {encoder.upper()}, CQ: {final_quality})...")

        source_duration = get_video_duration(work_input, ffprobe_bin=Path(FFPROBE_BIN))
        if source_duration > 0 and sample_duration > 0:
            measured_speed = 20.0 / sample_duration
            filter_mode = "nnedi_slow" if is_interlaced else "none"
            adjusted_speed = calculate_adjusted_speed_factor(measured_speed, filter_mode)
            estimated_duration = estimate_total_duration(source_duration, adjusted_speed)
            eta_timestamp = time.time() + estimated_duration
            eta_local = time.strftime("%H:%M", time.localtime(eta_timestamp))
            duration_minutes, duration_seconds = divmod(int(round(estimated_duration)), 60)
            logger.info(
                "Geschätzte Dauer %02d:%02d, wahrscheinliche ETA %s",
                duration_minutes,
                duration_seconds,
                eta_local,
            )

        save_manifest(job_data)

        enc_args = build_encoder_args(
            input_path=work_input,
            output_path=work_output,
            encoder=encoder,
            codec=codec,
            quality_value=final_quality,
            ai_choice=ai_mode,
            audio_mode="copy",
            subtitle_burn=forced_subs,
            subtitle_forced_only=subtitle_forced_only,
            use_nnedi=is_interlaced,
            subtitle_track=forced_subtitle_track,
            subtitle_codec=forced_subtitle_codec,
        )

        # Encoder-Meldungen direkt in unseren job-spezifischen Logger leiten
        run_command(enc_args, logger=logger)

        # SCHRITT 4: Abschluss
        duration_sec = round(time.time() - start_time, 1)
        mins, secs = divmod(int(duration_sec), 60)
        formatted_duration = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

        job_data["step"] = "COMPLETED"
        job_data["status"] = "finished"
        job_data["completed_at"] = time.time()
        job_data["duration_sec"] = duration_sec
        save_manifest(job_data)

        # Schicke Konsolen- & Log-Zusammenfassung
        summary = (
            f"\n"
            f"========================================================\n"
            f" 🎉 JOB ERFOLGREICH ABGESCHLOSSEN\n"
            f" --------------------------------------------------------\n"
            f"  Datei:        {input_path.name}\n"
            f"  Ziel-Codec:   {codec.upper()} ({encoder})\n"
            f"  Gewählter CQ: {final_quality}\n"
            f"  Denoise-Mode: {preflight_plan.get('denoise_mode')}\n"
            f"  Dauer:        {formatted_duration}\n"
            f"========================================================"
        )
        logger.info(summary)

    except Exception as err:
        logger.error(f"Fehler in Pipeline {job_id}: {err}", exc_info=True)
        job_data["step"] = "FAILED"
        job_data["status"] = "error"
        job_data["error_message"] = str(err)
        save_manifest(job_data)
        raise err

    finally:
        cleanup_vmaf_samples(WORK_DIR, input_path.stem)

        # File-Handler schließen, damit die Log-Datei zum Verschieben freigegeben wird
        file_handler.close()
        logger.removeHandler(file_handler)
        logger.removeHandler(console_handler)

        # 5. Aufräumen: Alles zusammen nach Results/ bzw. Done/ verschieben
        RESULT_DIR.mkdir(parents=True, exist_ok=True)

        # Fertige Mediendatei nach Result/
        if work_output.exists():
            shutil.move(str(work_output), str(RESULT_DIR / work_output.name))
        # Manifest & Log mit identischem Vornamen nach Result/
        if job_json_path.exists():
            shutil.move(str(job_json_path), str(RESULT_DIR / job_json_path.name))
        if job_log_path.exists():
            shutil.move(str(job_log_path), str(RESULT_DIR / job_log_path.name))

        # Bei Inbox-Dateien erst nach Erfolg das Original nach DONE_DIR verschieben.
        # Bei CLI-Dateien löschen wir nur die temporäre Kopie aus Work/.
        if work_input.exists():
            if is_from_inbox:
                if job_data.get("status") == "finished":
                    DONE_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(input_path), str(DONE_DIR / input_path.name))
                work_input.unlink()
            else:
                try:
                    work_input.unlink()
                except Exception as e:
                    logger.warning(f"Konnte Temp-Datei in Work/ nicht löschen: {e}")
        print("\n👀 Watcher bezieht wieder Stellung und wartet auf neue Dateien...\n")
