import asyncio
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    from .config import CONFIG
except ImportError:  # pragma: no cover
    from config import CONFIG

# Logger
logger = logging.getLogger("omni_pipeline")

app = FastAPI(title="Omni-Transcoder API", version="1.0")

# CORS freischalten, damit das Frontend uneingeschränkt zugreifen kann
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Globaler Job-Status
CURRENT_JOB: Dict[str, Any] = {
    "running": False,
    "filename": None,
    "process": None
}

# WebSocket Manager für Live-Logs
class LogWebSocketManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                self.disconnect(connection)

ws_manager = LogWebSocketManager()


# Custom Logging Handler zur Umleitung aller omni_pipeline Logs an WebSockets
class WebSocketLogHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                asyncio.create_task(ws_manager.broadcast(log_entry))
        except RuntimeError:
            pass

# Handler registrieren
ws_handler = WebSocketLogHandler()
ws_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(ws_handler)


# --- REST ENDPUNKTE ---

@app.get("/config")
async def get_config():
    """Liefert die Standardkonfiguration für das Frontend."""
    return {
        "default_codec": CONFIG.get("default_codec", "av1"),
        "default_ai_choice": CONFIG.get("default_ai_choice", "2")
    }


@app.get("/status")
async def get_status():
    """Statusabfrage nach Page-Reload (F5)."""
    return {
        "running": CURRENT_JOB["running"],
        "filename": CURRENT_JOB["filename"]
    }


@app.post("/start-encode")
async def start_encode(
    file: UploadFile = File(...),
    codec: str = Form("av1"),
    hdr_mode: str = Form("2")
):
    """Nimmt Standard-Uploads entgegen und speichert sie direkt im Input-Ordner."""
    if CURRENT_JOB["running"]:
        raise HTTPException(status_code=400, detail="Es läuft bereits ein Transcoding-Prozess.")

    input_dir = Path(CONFIG["input_dir"])
    target_path = input_dir / file.filename

    logger.info(f"Empfange Datei für Transcoding: {file.filename} (Codec: {codec}, AI-Choice: {hdr_mode})")

    try:
        with open(target_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        CURRENT_JOB["running"] = True
        CURRENT_JOB["filename"] = file.filename
        
        logger.info(f"Datei erfolgreich im Input-Ordner abgelegt: {target_path}")
        # Hier wird die eigentliche Pipeline asynchron angestoßen
        return {"status": "started", "filename": file.filename}

    except Exception as e:
        logger.error(f"Fehler beim Speichern der Upload-Datei: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload-chunk")
async def upload_chunk(
    file: UploadFile = File(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...)
):
    """Ermöglicht Chunked-Uploads für riesige Videodateien."""
    temp_dir = Path(CONFIG["work_dir"]) / "chunks" / filename
    temp_dir.mkdir(parents=True, exist_ok=True)

    chunk_file = temp_dir / f"chunk_{chunk_index:05d}"
    
    with open(chunk_file, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    logger.info(f"Chunk {chunk_index + 1}/{total_chunks} für {filename} empfangen.")

    # Wenn alle Chunks da sind -> Zusammenfügen & in Input schieben
    if len(list(temp_dir.glob("chunk_*"))) == total_chunks:
        final_input_path = Path(CONFIG["input_dir"]) / filename
        logger.info(f"Alle Chunks empfangen. Setze {filename} zusammen...")

        with open(final_input_path, "wb") as outfile:
            for i in range(total_chunks):
                part_path = temp_dir / f"chunk_{i:05d}"
                with open(part_path, "rb") as partfile:
                    outfile.write(partfile.read())

        # Chunks aufräumen
        shutil.rmtree(temp_dir)
        logger.info(f"Datei {filename} vollständig zusammengesetzt und bereit für Processing.")

    return {"status": "chunk_received", "chunk_index": chunk_index}


@app.post("/cancel-encode")
async def cancel_encode():
    """Bricht das laufende Transcoding ab."""
    if not CURRENT_JOB["running"]:
        return {"status": "idle", "message": "Kein Job aktiv."}

    logger.warning("Abbruchsignal vom Frontend empfangen!")
    
    # Prozess beenden falls vorhanden
    if CURRENT_JOB.get("process"):
        try:
            CURRENT_JOB["process"].kill()
        except Exception as e:
            logger.error(f"Fehler beim Beenden des Prozesses: {e}")

    CURRENT_JOB["running"] = False
    CURRENT_JOB["filename"] = None
    CURRENT_JOB["process"] = None

    logger.info("Transcoding-Job erfolgreich abgebrochen.")
    return {"status": "cancelled"}


# --- WEBSOCKET FÜR LIVE-LOGS ---

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await ws_manager.connect(websocket)
    logger.info("Web-Client hat sich mit dem Log-Stream verbunden.")
    try:
        while True:
            # Verbindung aufrecht erhalten (Keep-alive)
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logger.info("Web-Client hat Log-Stream getrennt.")