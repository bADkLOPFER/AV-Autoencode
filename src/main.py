import argparse
import sys
import logging
from pathlib import Path

from pipeline import process_job

# Optionales Logging für die CLI-Ebene
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("omni_cli")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Omni-Transcoder CLI – Videos einfach über die Kommandozeile verarbeiten."
    )
    
    # Eingabedatei (Pflichtfeld)
    parser.add_argument(
        "input_file",
        type=Path,
        help="Pfad zur Quelldatei (z.B. C:/Videos/film.mp4)"
    )

    # Optionale Argumente mit euren vereinbarten Standards (Default = config.json/default_codec)
    parser.add_argument(
        "-c", "--codec",
        type=str,
        default=None,
        choices=["av1", "hevc", "h264"],
        help="Ziel-Codec für die Transkodierung (Standard: aus config.json/default_codec)"
    )
    
    parser.add_argument(
        "-e", "--encoder",
        type=str,
        default="nvencc",
        choices=["nvencc", "qsvencc", "vceenc", "ffmpeg"],
        help="Zu verwendender Encoder (Standard: nvencc)"
    )

    parser.add_argument(
        "-q", "--quality",
        type=int,
        default=22,
        help="Ziel-Qualität (CQ / CRF Wert, Standard: 22)"
    )

    args = parser.parse_args()

    input_path: Path = args.input_file

    if not input_path.exists():
        logger.error(f"[X] Die Datei '{input_path}' wurde nicht gefunden!")
        sys.exit(1)

    logger.info(f"[>] CLI-Start: Verarbeite {input_path.name}")
    logger.info(f"[>] Einstellungen: Codec={(args.codec or 'config-default').upper()}, Encoder={args.encoder}, Quality={args.quality}")

    try:
        # Direkter Aufruf der überarbeiteten Pipeline-Engine
        process_job(
            input_path=input_path,
            codec=args.codec,
            encoder=args.encoder,
            quality=args.quality
        )
        logger.info(f"[OK] CLI: Verarbeitung von '{input_path.name}' erfolgreich abgeschlossen!")

    except Exception as err:
        logger.error(f"[X] CLI: Fehler bei der Verarbeitung von '{input_path.name}': {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()