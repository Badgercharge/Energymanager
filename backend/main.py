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

app = FastAPI()

# CORS
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# globaler Zustand
app.state.cps = {}
# Eco-Config im Speicher (nur sunny_kw / cloudy_kw)
app.state.eco = {
    "sunny_kw": float(os.getenv("SUNNY_KW", "11.0")),
    "cloudy_kw": float(os.getenv("CLOUDY_KW", "3.7")),
}

# WebSocket Adapter
from fastapi import WebSocket
class FastAPIWebSocketAdapter:
    def __init__(self, ws: WebSocket):
        self.ws = ws
    async def recv(self) -> str:
        return await self.ws.receive_text()
    async def send(self, message: str):
        await self.ws.send_text(message)

@app.websocket("/ocpp/{cp_id}")
async def ocpp(ws: WebSocket, cp_id: str):
    await ws.accept(subprotocol="ocpp1.6")
    logging.info("WS connected: %s", cp_id)
    ocpp_ws = FastAPIWebSocketAdapter(ws)
    cp = CentralSystem(cp_id, ocpp_ws)
    app.state.cps[cp_id] = cp
    try:
        await cp.start()
    except Exception as e:
        logging.exception("WS error %s: %s", cp_id, e)
        raise
    finally:
        logging.info("WS disconnected: %s", cp_id)
        if cp_id in STATE:
            STATE[cp_id].connected = False
        app.state.cps.pop(cp_id, None)

# REST: Points
@app.get("/api/points")
def list_points():
    return [vars(s) for s in STATE.values()]

@app.post("/api/points/{cp_id}/mode/{mode}")
def set_mode(cp_id: str, mode: str):
    assert mode in ["eco", "max", "off"]
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

# SoC manuell setzen (Fallback neben OCPP-Auto-SoC)
@app.post("/api/points/{cp_id}/soc")
def set_soc(cp_id: str, soc: int = Body(..., embed=True)):
    if cp_id not in STATE:
        from models import ChargePointState
        STATE[cp_id] = ChargePointState(id=cp_id)
    STATE[cp_id].current_soc = int(soc)
    STATE[cp_id].soc = int(soc)
    return {"ok": True}

# Boost im Eco – pro Lader
@app.get("/api/points/{cp_id}/boost")
def get_boost(cp_id: str):
    s = STATE.get(cp_id)
    if not s:
        return {"enabled": False}
    return {
        "enabled": s.boost_enabled,
        "cutoff_local": s.boost_cutoff_local,
        "target_soc": s.boost_target_soc,
    }

@app.post("/api/points/{cp_id}/boost")
def set_boost(
    cp_id: str,
    enabled: bool = Body(...),
    cutoff_local: str = Body(..., embed=True),
    target_soc: int = Body(...),
):
    from models import ChargePointState
    s = STATE.get(cp_id) or ChargePointState(id=cp_id)
    s.boost_enabled = bool(enabled)
    s.boost_cutoff_local = cutoff_local
    s.boost_target_soc = int(target_soc)
    STATE[cp_id] = s
    # Boost ist eine Erweiterung von Eco; Modus bleibt Eco
    if s.mode == "off":
        s.mode = "eco"
    return {"ok": True}

# Eco-Config (nur sunny_kw / cloudy_kw)
@app.get("/api/config/eco")
def get_eco():
    return app.state.eco

@app.post("/api/config/eco")
def post_eco(
    sunny_kw: float = Body(...),
    cloudy_kw: float = Body(...),
):
    app.state.eco["sunny_kw"] = float(sunny_kw)
    app.state.eco["cloudy_kw"] = float(cloudy_kw)
    return {"ok": True, **app.state.eco}

@app.get("/")
def root():
    return {"ok": True, "msg": "EMS backend running"}

# Startup-Loop
@app.on_event("startup")
async def on_start():
    lat = float(os.getenv("LAT", "48.87"))
    lon = float(os.getenv("LON", "12.65"))
    base_limit_kw = float(os.getenv("BASE_LIMIT_KW", "11"))
    asyncio.create_task(control_loop(app, lat, lon, base_limit_kw))

# Statische Dateien optional
if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
