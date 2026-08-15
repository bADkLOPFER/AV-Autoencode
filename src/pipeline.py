import json
import shutil
import time
from pathlib import Path
from typing import Dict, Any

from paths import WORK_DIR, RESULT_DIR
from media_analysis import analyze_media, has_forced_subtitles
from encoding import build_encoder_args, run_command
from utils import logger

def update_job_status(job_file: Path, data: Dict[str, Any]):
    """Schreibt den aktuellen Status atomic in die .job.json-Datei."""
    job_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def process_job(inbox_path: Path):
    job_id = inbox_path.stem
    work_input = WORK_DIR / inbox_path.name
    work_output = WORK_DIR / f"{job_id}_encoded.mkv"
    job_json_path = WORK_DIR / f"{job_id}.job.json"

    job_data = {
        "job_id": job_id,
        "input_file": inbox_path.name,
        "status": "PROCESSING",
        "step": "INITIALIZING",
        "start_time": time.time(),
        "vmaf_score": None,
        "error": None
    }

    try:
        # 1. Verschiebe Quelldatei von Inbox/ nach Work/
        update_job_status(job_json_path, job_data)
        shutil.move(str(inbox_path), str(work_input))

        # 2. Analyse
        job_data["step"] = "ANALYZING"
        update_job_status(job_json_path, job_data)
        media_info = analyze_media(work_input)
        forced_subs = has_forced_subtitles(work_input)

        # 3. Transkodierung
        job_data["step"] = "ENCODING"
        update_job_status(job_json_path, job_data)

        # Nutzen der bestehenden build_encoder_args aus encoding.py
        enc_args = build_encoder_args(
            encoder_choice="nvencc",
            codec="hevc",
            quality=22,
            input_path=work_input,
            output_path=work_output,
            audio_mode="copy",
            subtitle_burn=forced_subs,
        )
        
        run_command(enc_args)

        # 4. Finalisierung: Verschieben nach Result/
        job_data["step"] = "COMPLETING"
        update_job_status(job_json_path, job_data)

        final_output = RESULT_DIR / work_output.name
        final_json = RESULT_DIR / f"{job_id}.job.json"

        shutil.move(str(work_output), str(final_output))
        
        job_data["status"] = "FINISHED"
        job_data["step"] = "DONE"
        job_data["end_time"] = time.time()
        job_data["duration_sec"] = round(job_data["end_time"] - job_data["start_time"], 2)
        
        update_job_status(final_json, job_data)

        # Aufräumen im Work-Verzeichnis
        if work_input.exists():
            work_input.unlink()
        if job_json_path.exists():
            job_json_path.unlink()

        print(f"[✓] Job erfolgreich abgeschlossen: {final_output.name}")

    except Exception as exc:
        logger.exception(f"Fehler in Job {job_id}: {exc}")
        job_data["status"] = "FAILED"
        job_data["error"] = str(exc)
        update_job_status(job_json_path, job_data)