import asyncio, logging, os
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from ocpp_cs import CentralSystem
from models import STATE
from scheduler import control_loop

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = FastAPI()

# Für den Start offen lassen; später auf deine Vercel-Domain einschränken
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.cps = {}

# Minimaler Adapter, der dem OCPP-ChargePoint ein send/recv-Interface bietet
class FastAPIWebSocketAdapter:
    def __init__(self, ws: WebSocket):
        self.ws = ws
    async def recv(self) -> str:
        # erwartet Textframes
        return await self.ws.receive_text()
    async def send(self, message: str):
        await self.ws.send_text(message)

@app.websocket("/ocpp/{cp_id}")
async def ocpp(ws: WebSocket, cp_id: str):
    # ocpp>=2.x: Subprotocol-String direkt verwenden
    await ws.accept(subprotocol="ocpp1.6")
    ocpp_ws = FastAPIWebSocketAdapter(ws)
    cp = CentralSystem(cp_id, ocpp_ws)
    app.state.cps[cp_id] = cp
    try:
        await cp.start()
    finally:
        if cp_id in STATE:
            STATE[cp_id].connected = False
        app.state.cps.pop(cp_id, None)

@app.get("/api/points")
def list_points():
    return [vars(s) for s in STATE.values()]

@app.post("/api/points/{cp_id}/mode/{mode}")
def set_mode(cp_id: str, mode: str):
    assert mode in ["eco","max","off","schedule"]
    if cp_id not in STATE:
        from models import ChargePointState
        STATE[cp_id] = ChargePointState(id=cp_id)
    STATE[cp_id].mode = mode
    return {"ok": True}

@app.post("/api/points/{cp_id}/limit")
def set_limit(cp_id: str, kw: float):
    if cp_id not in STATE:
        from models import ChargePointState
        STATE[cp_id] = ChargePointState(id=cp_id)
    STATE[cp_id].target_kw = kw
    return {"ok": True}

@app.get("/")
def root():
    return {"ok": True, "msg": "EMS backend running"}

@app.on_event("startup")
async def on_start():
    lat = float(os.getenv("LAT", "48.87"))  # grob Radldorf
    lon = float(os.getenv("LON", "12.65"))
    base_limit_kw = float(os.getenv("BASE_LIMIT_KW", "11"))
    min_grid_kw = float(os.getenv("MIN_GRID_KW", "0.5"))
    asyncio.create_task(control_loop(app, lat, lon, base_limit_kw, min_grid_kw))

# Optional: falls /static existiert, Frontend-Assets direkt ausliefern
if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
