import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI()

# Read default port from config.py
DEFAULT_WEB_PORT = 8265

def find_free_port():
    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: None, '127.0.0.1', DEFAULT_WEB_PORT)
    port = server.sockets[0].getsockname()[1]
    server.close()
    return port

@app.on_event("startup")
async def startup_event():
    global DEFAULT_WEB_PORT
    while True:
        try:
            await uvicorn.run(app, host="127.0.0.1", port=DEFAULT_WEB_PORT)
        except OSError as e:
            DEFAULT_WEB_PORT += 1
            print(f"Port {DEFAULT_WEB_PORT} is already in use. Trying next...")
    
@app.post("/start-encode")
async def start_encode(request: Request, input_path: str, codec: str, ai_choice: str, denoise: bool, subtitle_burn: bool):
    logger.info(f"Starting encode with parameters: input_path={input_path}, codec={codec}, ai_choice={ai_choice}, denoise={denoise}, subtitle_burn={subtitle_burn}")
    # TODO: Implement encoding logic here
    return JSONResponse(content={"message": "Encoding started successfully"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = find_free_port()
    print(f"Listening on port {port}")
    uvicorn.run(app, host="127.0.0.1", port=port)