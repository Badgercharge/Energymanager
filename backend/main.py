# backend/main.py
import os
import logging
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ocpp_cs import CentralSystem, extract_cp_id_from_path, KNOWN_CP_IDS, cp_status, cp_registry

log = logging.getLogger("uvicorn")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

def parse_origins(val: str) -> List[str]:
    if not val:
        return ["*"]
    return [o.strip() for o in val.split(",") if o.strip()]

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
ALLOWED_ORIGINS = parse_origins(FRONTEND_ORIGIN)

app = FastAPI(title="HomeCharger Backend", version="1.0.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WICHTIG: Static NICHT auf "/" mounten
if os.path.isdir("static"):
    app.mount("/app", StaticFiles(directory="static", html=True), name="static")

class StarletteWSAdapter:
    def __init__(self, ws: WebSocket):
        self.ws = ws
    async def recv(self) -> str:
        return await self.ws.receive_text()
    async def send(self, data: str):
        await self.ws.send_text(data)
    async def close(self, code: int = 1000, reason: str = ""):
        try:
            await self.ws.close(code=code)
        except Exception:
            pass

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/points")
async def api_points():
    return list(cp_status.values())

# Debug-Helfer (temporär): zeigt alle registrierten Routen
@app.get("/debug/routes")
def debug_routes():
    return sorted([r.path for r in app.router.routes])

# OCPP-WS: akzeptiere /ocpp, /ocpp/{cp_id}, /ocpp/{cp_id}/{tail}
@app.websocket("/ocpp")
@app.websocket("/ocpp/{cp_id}")
@app.websocket("/ocpp/{cp_id}/{tail:path}")
async def ocpp_ws(ws: WebSocket, cp_id: str | None = None, tail: str | None = None):
    try:
        await ws.accept(subprotocol="ocpp1.6")
    except Exception:
        await ws.accept()

    if not cp_id:
        cp_id = extract_cp_id_from_path("/ocpp")

    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        log.warning("Reject OCPP for unknown CP-ID: %s (tail=%s)", cp_id, tail)
        await ws.close(code=4000)
        return

    log.info("OCPP connect (ASGI): %s (tail=%s)", cp_id, tail)
    adapter = StarletteWSAdapter(ws)
    cp = CentralSystem(cp_id, adapter)
    cp_registry[cp_id] = cp
    try:
        await cp.start()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("OCPP ASGI session error (%s): %s", cp_id, e)
    finally:
        cp_registry.pop(cp_id, None)
        log.info("OCPP disconnect (ASGI): %s", cp_id)

@app.get("/")
async def root():
    return JSONResponse({"message": "HomeCharger Backend up. See /api/points and /ocpp/{cp_id}."})
