# video_tool/server.py
import socket
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect, File, Form, UploadFile
from pydantic import BaseModel
from typing import List
from main import run_encoding_job
import uvicorn
import asyncio
import logging
import shutil
import job_state
import subprocess # Hinzufügen, falls noch nicht vorhanden
import signal
from paths import PATHS
from pathlib import Path

from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)
ACTIVE_PROCESS = None

# Read default port from config.py
try:
    from config import DEFAULT_WEB_PORT, WORKFLOW_DEFAULTS
except ImportError:
    DEFAULT_WEB_PORT = 8265

default_hdr_string = "truehdr" if WORKFLOW_DEFAULTS.get("default_true_hdr", True) else "normal"

# Globaler Event-Loop-Speicher
server_loop = None

INITIAL_CONFIG = {
    "codecs": ["hevc", "av1"],
    "hdr_modes": ["normal", "truehdr"],
    "defaults": {
        "codec": WORKFLOW_DEFAULTS.get("default_codec", "av1"),
        "hdr_mode": default_hdr_string
    }
}

results_dir = PATHS.get("results", Path("Results"))
WORK_DIR = results_dir / "Work"
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Ganz oben nach dem FastAPI-App-Start:
templates = Jinja2Templates(directory="templates")

runtime_config = INITIAL_CONFIG.copy()

class EncodeRequest(BaseModel):
    input_path: str
    codec: str = "av1"
    hdr_mode: str = "truehdr"

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)

manager = ConnectionManager()

class StdoutRedirector:
    def __init__(self, original_stdout):
        self.original_stdout = original_stdout

    def write(self, text):
        # 1. Ganz normal ins VSC-Terminal schreiben
        self.original_stdout.write(text)
        self.original_stdout.flush()
        
        # 2. An den Browser per WebSocket streamen (wenn Text vorhanden & Loop läuft)
        cleaned = text.strip()
        if cleaned and server_loop and server_loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(manager.broadcast(cleaned), server_loop)
            except Exception:
                pass

    def flush(self):
        self.original_stdout.flush()

# --- MODERNES LIFESPAN MANAGMENT (Ersetzt on_event) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global server_loop
    server_loop = asyncio.get_running_loop()
    sys.stdout = StdoutRedirector(sys.stdout)
    yield
    # Hier könnten Cleanup-Aktionen beim Herunterfahren des Servers stattfinden

app = FastAPI(lifespan=lifespan)

@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text() # Keep-alive
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/")
async def read_root(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"request": request}
    )

@app.get("/config")
async def get_config():
    # Liefert den aktuellen Zustand aus dem Speicher
    return runtime_config

@app.post("/start-encode")
async def start_encode(
    request: Request,
    file: UploadFile = File(...),
    codec: str = Form("av1"),
    ai_choice: str = Form("2"),
    background_tasks: BackgroundTasks = None
):
    global ACTIVE_PROCESS
    # 1. Dateigröße aus dem Content-Length Header ermitteln
    content_length = request.headers.get("content-length")
    if content_length:
        total_request_bytes = int(content_length)
        file_size_gb = total_request_bytes / (1024**3)
        
        # 2. Speicherplatz auf der Zieldisk prüfen
        target_disk = WORK_DIR.anchor
        disk_usage = shutil.disk_usage(target_disk)
        free_space_gb = disk_usage.free / (1024**3)
        
        multiplier = WORKFLOW_DEFAULTS.get("disk_space_multiplier", 1.5)
        required_space_gb = file_size_gb * multiplier

        if free_space_gb < required_space_gb:
            return JSONResponse(
                content={
                    "status": "error", 
                    "message": f"Zu wenig Speicherplatz! Frei: {free_space_gb:.1f} GB, Benötigt: ~{required_space_gb:.1f} GB"
                },
                status_code=400
            )

    # 3. Datei im zentralen Work-Ordner speichern
    original_path = Path(file.filename)
    max_stem_length = 100
    safe_stem = original_path.stem[:max_stem_length]

    staged_filename = f"{safe_stem}_uploaded{original_path.suffix}"
    staged_file_path = WORK_DIR / staged_filename

    with open(staged_file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # 4. Payload für den Wrapper vorbereiten
    payload = {
        "input_path": str(staged_file_path),
        "codec": codec,
        "ai_choice": ai_choice,
        "is_staged": True
    }
    
    background_tasks.add_task(run_encoding_job, payload)
    
    return JSONResponse(
        content={
            "status": "success", 
            "message": f"Speicherprüfung OK. Datei '{file.filename}' wird verarbeitet."
        },
        status_code=200
    )

@app.post("/cancel-encode")
async def cancel_encode():
    proc = job_state.get_process()
    if proc and proc.poll() is None:
        proc.terminate() # Prozess beenden
        job_state.set_process(None)
        # Sende "RESET"-Signal an alle verbundenen WebSockets
        await manager.broadcast("[JOB_CANCELLED]")
        return {"status": "success", "message": "Encoding abgebrochen."}
    return {"status": "error", "message": "Kein aktiver Job gefunden."}

def find_free_port(DEFAULT_WEB_PORT: int) -> int:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for port in range(DEFAULT_WEB_PORT, 10000):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue


if __name__ == "__main__":
    # 1. Freien Port GANZ VOR dem Start von uvicorn suchen
    port = find_free_port(DEFAULT_WEB_PORT)
    print(f"Listening on port {port}")
    # 2. Uvicorn sauber starten (ohne await!)
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)
