import time
import sys
import logging
import subprocess
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

try:
    from .config import load_config, verify_tools
    from .pipeline import process_job
except ImportError:  # pragma: no cover
    from config import load_config, verify_tools
    from pipeline import process_job

logger = logging.getLogger("omni_pipeline")
VALID_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m2ts"}


# Konfiguration laden & Tools prüfen
CONFIG = load_config()
FFPROBE_BIN = str(CONFIG.get("tools", {}).get("ffprobe", "ffprobe"))

if not verify_tools(CONFIG):
    print("❌ Fehler: Tool-Validierung fehlgeschlagen!")
    sys.exit(1)

def is_file_ready(file_path: Path, check_interval: int = 3, stable_checks: int = 2) -> bool:
    """Prüft stabile Größe, Änderungszeit und Lesbarkeit eines Videocontainers."""
    try:
        previous = None
        for _ in range(stable_checks):
            stat_result = file_path.stat()
            current = (stat_result.st_size, stat_result.st_mtime_ns)
            if current[0] <= 0 or previous != current:
                previous = current
                time.sleep(check_interval)
                continue

            time.sleep(check_interval)

        final_stat = file_path.stat()
        if final_stat.st_size <= 0 or previous != (final_stat.st_size, final_stat.st_mtime_ns):
            return False

        probe = subprocess.run(
            [
                FFPROBE_BIN,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return probe.returncode == 0 and bool(probe.stdout.strip())
    except (FileNotFoundError, PermissionError, OSError):
        return False

def start_watcher(poll_interval: int = 5):
    INBOX_DIR = Path(CONFIG["inbox_dir"])
    print(f"[+] Watcher gestartet. Überwache: {INBOX_DIR}")

    while True:
        try:
            for file_path in INBOX_DIR.iterdir():
                if file_path.is_file() and file_path.suffix.lower() in VALID_EXTENSIONS:
                    if file_path.name.startswith(".") or file_path.name.endswith(".tmp"):
                        continue

                    print(f"[>] Neue Datei erkannt: {file_path.name}. Prüfe Vollständigkeit...")
                    if is_file_ready(file_path):
                        print(f"[OK] Datei bereit: {file_path.name}. Starte Transcoding-Pipeline...")
                        try:
                            process_job(input_path=file_path)  # Codec kommt aus config.json (default_codec)
                            logger.info(f"[OK] Transcoding-Pipeline abgeschlossen für: {file_path.name}")
                        except PermissionError:
                            print(f"[...] Datei ist noch gesperrt: {file_path.name}. Neuer Versuch im nächsten Durchlauf.")
                    else:
                        print(f"[...] Datei wird noch geschrieben: {file_path.name}")
        except Exception as err:
            print(f"[X] Watcher-Fehler: {err}")

        time.sleep(poll_interval)

if __name__ == "__main__":
    start_watcher()