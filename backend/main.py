# backend/main.py
import os
import logging
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# OCPP-Server-Seite (deine ocpp_cs.py)
from ocpp_cs import CentralSystem, extract_cp_id_from_path, KNOWN_CP_IDS, cp_status

log = logging.getLogger("uvicorn")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

def parse_origins(val: str) -> List[str]:
    if not val:
        return ["*"]
    return [o.strip() for o in val.split(",") if o.strip()]

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
ALLOWED_ORIGINS = parse_origins(FRONTEND_ORIGIN)

app = FastAPI(title="HomeCharger Backend", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static Files NICHT auf "/" mounten, um /api und /ocpp nicht zu überschreiben
if os.path.isdir("static"):
    app.mount("/app", StaticFiles(directory="static", html=True), name="static")

# Adapter: macht Starlette WebSocket kompatibel zu ocpp.ChargePoint (send/recv)
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

@app.on_event("startup")
async def on_startup():
    log.info("Startup: FRONTEND_ORIGIN=%s", ALLOWED_ORIGINS)
    log.info("Startup: KNOWN_CP_IDS=%s", ",".join(sorted(KNOWN_CP_IDS)) if KNOWN_CP_IDS else "(none)")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/version")
async def version():
    return {"name": "homecharger-backend", "version": "1.0.0"}

@app.get("/api/points")
async def api_points():
    # Gibt alle bekannten CP-Status als Liste zurück
    # Beispiel-Felder: id, status, power_w, last_seen, session{start_time, kwh, ...}
    return list(cp_status.values())

# OCPP 1.6 WebSocket-Routen
@app.websocket("/ocpp")
@app.websocket("/ocpp/{cp_id}")
async def ocpp_ws(ws: WebSocket, cp_id: str | None = None):
    # OCPP Subprotocol aushandeln
    # Viele Boxen erwarten "ocpp1.6"
    try:
        await ws.accept(subprotocol="ocpp1.6")
    except Exception:
        # Fallback (akzeptieren ohne Subprotocol, falls Gerät keines sendet)
        await ws.accept()

    # CP-ID aus Pfad oder Default ableiten
    if not cp_id:
        cp_id = extract_cp_id_from_path("/ocpp")

    # Whitelist prüfen
    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        log.warning("Reject OCPP for unknown CP-ID: %s", cp_id)
        await ws.close(code=4000)
        return

    log.info("OCPP connect (ASGI): %s", cp_id)
    adapter = StarletteWSAdapter(ws)
    cp = CentralSystem(cp_id, adapter)

    try:
        # Start blockiert bis der Client die Verbindung schließt
        await cp.start()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("OCPP ASGI session error (%s): %s", cp_id, e)
    finally:
        log.info("OCPP disconnect (ASGI): %s", cp_id)

# Optional: Root zeigt minimalen Hinweis (damit "/" nicht 404 ist)
@app.get("/")
async def root():
    return JSONResponse({"message": "HomeCharger Backend up. See /api/points and /ocpp/{cp_id}."})
