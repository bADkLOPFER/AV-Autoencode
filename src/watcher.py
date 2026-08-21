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
    """Prüft stabile Größe und Lesbarkeit eines Videocontainers.

    Nur die Dateigröße wird verglichen: st_mtime_ns ist auf manchen
    Netzwerkfreigaben (SMB/NFS) leicht instabil und würde die Erkennung
    sonst nie konvergieren lassen, obwohl die Größe längst konstant ist.
    """
    try:
        stable_count = 0
        last_size = -1
        while stable_count < stable_checks:
            size = file_path.stat().st_size
            if size > 0 and size == last_size:
                stable_count += 1
            else:
                stable_count = 0
            last_size = size
            time.sleep(check_interval)

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
                        print(f"[OK] Datei bereit: {file_path.name}. Kopiere zuerst lokal nach Work...")
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