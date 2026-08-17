import time
import sys
import logging
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

if not verify_tools(CONFIG):
    print("❌ Fehler: Tool-Validierung fehlgeschlagen!")
    sys.exit(1)

def is_file_ready(file_path: Path, check_interval: int = 3) -> bool:
    """Stellt sicher, dass der Kopiervorgang in Inbox/ abgeschlossen ist."""
    try:
        size_before = file_path.stat().st_size
        time.sleep(check_interval)
        size_after = file_path.stat().st_size
        return size_before == size_after and size_before > 0
    except FileNotFoundError:
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
                        process_job(
                            input_path=file_path,
                            codec="av1"  # Nimmt automatisch AV1
                        )
                        logger.info(f"[OK] Transcoding-Pipeline abgeschlossen für: {file_path.name}")
                    else:
                        print(f"[...] Datei wird noch geschrieben: {file_path.name}")
        except Exception as err:
            print(f"[X] Watcher-Fehler: {err}")

        time.sleep(poll_interval)

if __name__ == "__main__":
    start_watcher()