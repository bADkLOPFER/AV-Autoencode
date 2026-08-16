# src/pipeline.py
from __future__ import annotations
from typing import Optional
import json
import logging
import os
import shutil
import time
import uuid
import sys
import re
from pathlib import Path

from paths import WORK_DIR, RESULT_DIR, DONE_DIR, INBOX_DIR
from media_analysis import analyze_media, analyze_noise_and_quality, has_forced_subtitles, calibrate_quality_vmaf, recommend_quality_value
from encoding import build_encoder_args, run_command
from config import DEFAULT_ENCODER, resolve_encoder_choice


def process_job(
    input_path: Path,
    codec: str = "av1",
    encoder: Optional[str] = None,
    quality: int = 22
) -> None:
    encoder = resolve_encoder_choice(encoder)
    # 1. Eindeutige Job-ID erzeugen
    clean_stem = re.sub(r'[^\w\-_.]', '_', input_path.stem).strip('_')
    job_id = f"{clean_stem}_{uuid.uuid4().hex[:6]}"

    # Pfade definieren: filmname_a1b2c3.job.json & filmname_a1b2c3.log
    job_json_path = WORK_DIR / f"{job_id}.job.json"
    job_log_path = WORK_DIR / f"{job_id}.log"
    work_input = WORK_DIR / input_path.name
    work_output = WORK_DIR / f"{clean_stem}_encoded.mkv"

    # Prüfen, ob die Datei aus der INBOX kommt
    is_from_inbox = (INBOX_DIR.resolve() in input_path.resolve().parents) or (input_path.parent.resolve() == INBOX_DIR.resolve())

    if is_from_inbox:
        # Kommt aus INBOX: Verschieben nach Work (Watcher-Prozess)
        if input_path.resolve() != work_input.resolve():
            shutil.move(str(input_path), str(work_input))
    else:
        # Kommt von der CLI / einem externen Pfad: Kopieren nach Work, damit Original erhalten bleibt
        if input_path.resolve() != work_input.resolve():
            shutil.copy2(str(input_path), str(work_input))

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
        media_info = analyze_media(work_input)
        forced_subs = has_forced_subtitles(media_info.get("subtitle_streams", []))

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
            )
        
        logger.info(f"VMAF-Analyse beendet. Optimaler CQ/CRF-Wert: {final_quality}")
        job_data["computed_cq"] = final_quality

        # SCHRITT 3: Transkodierung
        job_data["step"] = "ENCODING"
        logger.info(f"Starte Haupt-Encoding ({codec.upper()} via {encoder.upper()}, CQ: {final_quality})...")
        save_manifest(job_data)

        enc_args = build_encoder_args(
            input_path=work_input,
            output_path=work_output,
            encoder=encoder,
            codec=codec,
            quality_value=final_quality,
            audio_mode="copy",
            subtitle_burn=forced_subs,
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

        # Bei Inbox-Dateien schieben wir das verarbeitete Original nach DONE_DIR
        # Bei CLI-Dateien löschen wir nur die temporäre Kopie aus Work/
        if work_input.exists():
            if is_from_inbox:
                DONE_DIR.mkdir(parents=True, exist_ok=True)
                shutil.move(str(work_input), str(DONE_DIR / work_input.name))
            else:
                try:
                    work_input.unlink()
                except Exception as e:
                    logger.warning(f"Konnte Temp-Datei in Work/ nicht löschen: {e}")
        print("\n👀 Watcher bezieht wieder Stellung und wartet auf neue Dateien...\n")
