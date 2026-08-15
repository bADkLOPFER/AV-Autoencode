# server.py
import asyncio
import subprocess
import sys
import shutil
import logging
import os
import json
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from typing import List

# Importe aus dem Projekt
from paths import WORK_DIR, RESULT_DIR, PATHS

# Importiere Defaults sicher
try:
    from config import DEFAULT_WEB_PORT, WORKFLOW_DEFAULTS
except ImportError:
    # Fallback, falls config.py nicht gefunden wird
    DEFAULT_WEB_PORT = 8265
    WORKFLOW_DEFAULTS = {
        "default_codec": "av1", 
        "default_true_hdr": True, 
        "disk_space_multiplier": 1.5
    }

# --- CONFIG INIT ---
default_hdr_string = "truehdr" if WORKFLOW_DEFAULTS.get("default_true_hdr", True) else "normal"
INITIAL_CONFIG = {
    "codecs": ["hevc", "av1"],
    "hdr_modes": ["normal", "truehdr"],
    "defaults": {
        "codec": WORKFLOW_DEFAULTS.get("default_codec", "av1"),
        "hdr_mode": default_hdr_string
    }
}

# --- SETUP ---
logger = logging.getLogger(__name__)
ACTIVE_PROCESS = None
CANCEL_REQUESTED = False
results_dir = PATHS.get("results", Path("Results"))
WORK_DIR = results_dir / "Work"
WORK_DIR.mkdir(parents=True, exist_ok=True)

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

async def stream_and_broadcast(stream: asyncio.StreamReader):
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            
            # Robustes Decodieren: Erst UTF-8 versuchen, bei Fehler auf cp1252 ausweichen
            try:
                text = line.decode('utf-8').strip()
            except UnicodeDecodeError:
                text = line.decode('cp1252', errors='replace').strip()
                
            if text:
                await manager.broadcast(text)
    except Exception as e:
        logger.error(f"Stream-Fehler: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if ACTIVE_PROCESS:
        ACTIVE_PROCESS.terminate()

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

# --- ENDPUNKTE ---
@app.get("/jobs")
async def get_all_jobs():
    """
    Liest alle aktiven Jobs aus Work/ und alle abgeschlossenen aus Result/.
    Stört keine bestehenden Routen und arbeitet rein lesend.
    """
    active_jobs = []
    completed_jobs = []

    # 1. Aktive Jobs aus Work/ auslesen
    if WORK_DIR.exists():
        for job_file in WORK_DIR.glob("*.job.json"):
            try:
                data = json.loads(job_file.read_text(encoding="utf-8"))
                active_jobs.append(data)
            except Exception as err:
                logger.warning(f"Konnte Job-Datei {job_file.name} nicht lesen: {err}")

    # 2. Abgeschlossene Jobs aus Result/ auslesen
    if RESULT_DIR.exists():
        for job_file in RESULT_DIR.glob("*.job.json"):
            try:
                data = json.loads(job_file.read_text(encoding="utf-8"))
                completed_jobs.append(data)
            except Exception as err:
                logger.warning(f"Konnte Result-Datei {job_file.name} nicht lesen: {err}")

    return {
        "status": "success",
        "active": active_jobs,
        "completed": completed_jobs
    }

@app.post("/upload-chunk")
async def upload_chunk(
    file: UploadFile = File(...),
    filename: str = Form(...),
    chunkIndex: int = Form(...),
    totalChunks: int = Form(...)
):
    save_path = WORK_DIR / filename
    
    # 'ab' = Append Binary (hängt Daten an, statt zu überschreiben)
    with open(save_path, "ab") as f:
        content = await file.read()
        f.write(content)
        
    # Wenn letzter Chunk, Encoding starten
    if chunkIndex == totalChunks - 1:
        # Hier triggerst du dann dein start_encode mit dem fertigen Pfad
        return {"status": "finished"}
        
    return {"status": "chunk_received"}

@app.get("/")
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})

@app.get("/config")
async def get_config():
    """Liefert die Konfiguration an das Frontend."""
    return INITIAL_CONFIG

@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/start-encode")
async def start_encode(request: Request):
    global ACTIVE_PROCESS, CANCEL_REQUESTED
    CANCEL_REQUESTED = False  
    
    try:
        form = await request.form()
        uploaded_file: UploadFile = form.get("file")
        codec = form.get("codec", "av1")
        hdr_mode = form.get("hdr_mode", "normal")
        
        # Mapping von index.html (hdr_mode) auf main.py (ai_choice)
        ai_choice = "2" if hdr_mode == "truehdr" else "1"
        
        if not uploaded_file:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Keine Datei übergeben."})
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": f"Fehler beim Lesen des Requests: {e}"})

    staged_file_path = WORK_DIR / f"{Path(uploaded_file.filename).stem}_uploaded{Path(uploaded_file.filename).suffix}"
    
    if staged_file_path.exists():
        staged_file_path.unlink()

    content_length = int(request.headers.get("content-length", 0))
    file_size_gb = content_length / (1024**3) if content_length > 0 else 0
    
    if file_size_gb > 0:
        free_space_gb = shutil.disk_usage(WORK_DIR.anchor).free / (1024**3)
        if free_space_gb < (file_size_gb * WORKFLOW_DEFAULTS.get("disk_space_multiplier", 1.5)):
            return JSONResponse(status_code=400, content={"status": "error", "message": "Zu wenig Speicherplatz."})

    # Datei kontrolliert in Blöcken speichern
    try:
        with open(staged_file_path, "wb") as buffer:
            while True:
                if CANCEL_REQUESTED:
                    buffer.close()
                    if staged_file_path.exists():
                        staged_file_path.unlink()
                    return JSONResponse(status_code=400, content={"status": "error", "message": "Upload abgebrochen."})
                
                chunk = await uploaded_file.read(1024 * 1024) 
                if not chunk:
                    break
                buffer.write(chunk)
    except Exception as e:
        if staged_file_path.exists():
            staged_file_path.unlink()
        return JSONResponse(status_code=400, content={"status": "error", "message": f"Fehler beim Speichern der Datei: {e}"})

    if CANCEL_REQUESTED or not staged_file_path.exists() or staged_file_path.stat().st_size == 0:
        if staged_file_path.exists():
            staged_file_path.unlink()
        return JSONResponse(status_code=400, content={"status": "error", "message": "Job abgebrochen oder Datei leer."})

    cmd = [
        sys.executable, "-u", "main.py",
        str(staged_file_path),
        "--codec", codec,
        "--ai-choice", ai_choice
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    ACTIVE_PROCESS = proc

    asyncio.create_task(stream_and_broadcast(proc.stdout))
    asyncio.create_task(stream_and_broadcast(proc.stderr))

    return {"status": "success", "message": "Encoding Prozess gestartet."}

async def cleanup_work_dir():
    """Löscht alle temporären Dateien und Ordner im Work-Verzeichnis."""
    await asyncio.sleep(0.3)
    try:
        if WORK_DIR.exists():
            for item in WORK_DIR.iterdir():
                if item.is_file():
                    item.unlink(missing_ok=True)
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
            print("[OK] Arbeitsverzeichnis (Work-Dir) durch Server bereinigt.")
    except Exception as e:
        print(f"Fehler beim Bereinigen des Arbeitsverzeichnisses: {e}")

@app.get("/status")
async def get_status():
    global ACTIVE_PROCESS
    is_running = ACTIVE_PROCESS is not None and ACTIVE_PROCESS.returncode is None
    return {"running": is_running}

@app.post("/cancel-encode")
async def cancel_encode():
    global ACTIVE_PROCESS, CANCEL_REQUESTED
    CANCEL_REQUESTED = True
    
    if ACTIVE_PROCESS is not None and ACTIVE_PROCESS.returncode is None:
            pid = ACTIVE_PROCESS.pid
            try:
                if sys.platform == "win32":
                    # Zwingt Windows dazu, den Prozess UND alle Kindprozesse (nvencc64, ffmpeg) sofort zu töten
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=True)
                else:
                    ACTIVE_PROCESS.terminate()
                    await asyncio.wait_for(ACTIVE_PROCESS.wait(), timeout=3.0)
            except Exception as e:
                print(f"Fehler beim Killen des Prozesses: {e}")
                try:
                    ACTIVE_PROCESS.kill()
                except:
                    pass
                    
            ACTIVE_PROCESS = None
            await cleanup_work_dir()
            await manager.broadcast("[JOB_CANCELLED]")
            return {"status": "success", "message": "Encoding-Prozess komplett abgebrochen."}
    
    # Falls kein Prozess aktiv war (z.B. während Upload)
    await cleanup_work_dir()
    await manager.broadcast("[JOB_CANCELLED]")
    return {"status": "success", "message": "Vorbereitung / Upload abgebrochen."}

if __name__ == "__main__":
    import uvicorn
    # Erhöhe das Limit für die Request-Größe (hier auf 10GB = 10 * 1024 * 1024 * 1024)
    # Damit wird der Parser nicht mehr bei großen Dateien vorzeitig abgebrochen.
    uvicorn.run(
        "server:app", 
        host="0.0.0.0", 
        port=DEFAULT_WEB_PORT, 
        reload=False,
        limit_concurrency=10,
        # Das Limit hier ist entscheidend:
        limit_max_requests=1000
    )