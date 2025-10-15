import asyncio, logging, os
from fastapi import FastAPI, WebSocket, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from ocpp_cs import CentralSystem
from models import STATE
from scheduler import control_loop

load_dotenv()
logging.basicConfig(level=logging.INFO)

# 1) app zuerst erstellen
app = FastAPI()

# 2) Middleware danach
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3) App-weit genutzter Zustand
app.state.cps = {}

# 4) WebSocket-Endpoint: hier wird app bereits existieren
class FastAPIWebSocketAdapter:
    def __init__(self, ws: WebSocket):
        self.ws = ws
    async def recv(self) -> str:
        return await self.ws.receive_text()
    async def send(self, message: str):
        await self.ws.send_text(message)

@app.websocket("/ocpp/{cp_id}")
async def ocpp(ws: WebSocket, cp_id: str):
    # ocpp1.6 Subprotocol direkt verwenden
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

# 5) HTTP-Endpoints
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

# 6) Schedule-/SoC-Endpoints
@app.get("/api/points/{cp_id}/schedule")
def get_schedule(cp_id: str):
    s = STATE.get(cp_id)
    if not s:
        return {"enabled": False}
    return {
        "enabled": s.schedule_enabled,
        "cutoff_local": s.cutoff_local,
        "target_soc": s.target_soc,
        "current_soc": s.current_soc,
        "battery_kwh": s.battery_kwh,
        "charge_efficiency": s.charge_efficiency,
    }

@app.post("/api/points/{cp_id}/schedule")
def set_schedule(
    cp_id: str,
    enabled: bool = Body(...),
    cutoff_local: str = Body(..., embed=True),
    target_soc: int = Body(...),
    battery_kwh: float = Body(...),
    charge_efficiency: float = Body(0.92),
):
    from models import ChargePointState
    s = STATE.get(cp_id) or ChargePointState(id=cp_id)
    s.schedule_enabled = enabled
    s.cutoff_local = cutoff_local
    s.target_soc = int(target_soc)
    s.battery_kwh = float(battery_kwh)
    s.charge_efficiency = float(charge_efficiency)
    STATE[cp_id] = s
    if enabled:
        s.mode = "schedule"
    return {"ok": True}

@app.post("/api/points/{cp_id}/soc")
def set_soc(cp_id: str, soc: int = Body(..., embed=True)):
    if cp_id not in STATE:
        from models import ChargePointState
        STATE[cp_id] = ChargePointState(id=cp_id)
    STATE[cp_id].current_soc = int(soc)
    STATE[cp_id].soc = int(soc)
    return {"ok": True}

# 7) Startup-Task am Ende anlegen (nutzt app, das es schon gibt)
@app.on_event("startup")
async def on_start():
    lat = float(os.getenv("LAT", "48.87"))   # Radldorf grob
    lon = float(os.getenv("LON", "12.65"))
    base_limit_kw = float(os.getenv("BASE_LIMIT_KW", "11"))
    min_grid_kw = float(os.getenv("MIN_GRID_KW", "0.5"))
    asyncio.create_task(control_loop(app, lat, lon, base_limit_kw, min_grid_kw))

# 8) Optional: statische Dateien
if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
