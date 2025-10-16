import asyncio, logging, os
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, WebSocket, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from ocpp_cs import CentralSystem
from models import STATE, ENERGY_LOGS
from scheduler import control_loop
from mailer import send_mail, fmt_ts

load_dotenv()
logging.basicConfig(level=logging.INFO)
app = FastAPI()

# CORS
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=ALLOW_ORIGINS, allow_methods=["*"], allow_headers=["*"])

# Zustand
app.state.cps = {}
app.state.eco = {
    "sunny_kw": max(3.7, min(11.0, float(os.getenv("SUNNY_KW", "11.0")))),
    "cloudy_kw": max(3.7, min(11.0, float(os.getenv("CLOUDY_KW", "3.7")))),
}

# WebSocket Adapter
class FastAPIWebSocketAdapter:
    def __init__(self, ws: WebSocket): self.ws = ws
    async def recv(self) -> str: return await self.ws.receive_text()
    async def send(self, message: str): await self.ws.send_text(message)

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
        asyncio.create_task(send_mail(f"[EMS] Backend-Fehler OCPP WS – {cp_id}", f"Ladepunkt: {cp_id}\nZeit: {fmt_ts()}\nFehler: {repr(e)}"))
        raise
    finally:
        logging.info("WS disconnected: %s", cp_id)
        if cp_id in STATE:
            STATE[cp_id].connected = False
        app.state.cps.pop(cp_id, None)

# API: Punkte
@app.get("/api/points")
def list_points():
    return [vars(s) for s in STATE.values()]

@app.post("/api/points/{cp_id}/mode/{mode}")
def set_mode(cp_id: str, mode: str):
    assert mode in ["eco", "max", "off", "price"]
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
    kw = max(3.7, min(11.0, float(kw)))
    STATE[cp_id].target_kw = kw
    cp = app.state.cps.get(cp_id)
    if cp:
        asyncio.create_task(cp.push_charging_profile(kw))
    return {"ok": True, "kw": kw}

# SoC manuell – deaktiviert (nur OCPP), erhalten für Kompatibilität -> 405-ähnlich
@app.post("/api/points/{cp_id}/soc")
def set_soc_disabled(cp_id: str):
    return {"ok": False, "error": "SoC ist nur read-only (OCPP)."}

# Boost (Eco) – weiterhin konfigurierbar (nur Eco nutzt es)
@app.get("/api/points/{cp_id}/boost")
def get_boost(cp_id: str):
    s = STATE.get(cp_id)
    if not s: return {"enabled": False}
    return {"enabled": s.boost_enabled, "cutoff_local": s.boost_cutoff_local, "target_soc": s.boost_target_soc}

@app.post("/api/points/{cp_id}/boost")
def set_boost(cp_id: str, enabled: bool = Body(...), cutoff_local: str = Body(..., embed=True), target_soc: int = Body(...)):
    from models import ChargePointState
    s = STATE.get(cp_id) or ChargePointState(id=cp_id)
    s.boost_enabled = bool(enabled)
    s.boost_cutoff_local = cutoff_local
    s.boost_target_soc = int(target_soc)
    s.boost_reached_notified = False
    STATE[cp_id] = s
    if s.mode == "off": s.mode = "eco"
    return {"ok": True}

# Eco-Config (nur sunny/cloudy, strikt geklemmt)
@app.get("/api/config/eco")
def get_eco(): return app.state.eco

@app.post("/api/config/eco")
def post_eco(sunny_kw: float = Body(...), cloudy_kw: float = Body(...)):
    app.state.eco["sunny_kw"] = max(3.7, min(11.0, float(sunny_kw)))
    app.state.eco["cloudy_kw"] = max(3.7, min(11.0, float(cloudy_kw)))
    return {"ok": True, **app.state.eco}

# Statistik kWh (wie zuvor)
def _sum_range(points, days):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    total = 0.0
    for cp_id, rows in points.items():
        if not rows: continue
        first = None; last = None
        for ts, kwh in rows:
            if ts >= since:
                if first is None: first = kwh
                last = kwh
        if first is not None and last is not None:
            total += max(0.0, last - first)
    return round(total, 2)

def _per_cp(points, days):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    out = {}
    for cp_id, rows in points.items():
        first = None; last = None
        for ts, kwh in rows:
            if ts >= since:
                if first is None: first = kwh
                last = kwh
        out[cp_id] = round(max(0.0, (last - first) if (first is not None and last is not None) else 0.0), 2)
    return out

@app.get("/api/stats")
def stats():
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": {"7d": _sum_range(ENERGY_LOGS,7), "30d": _sum_range(ENERGY_LOGS,30)},
        "by_point": {"7d": _per_cp(ENERGY_LOGS,7), "30d": _per_cp(ENERGY_LOGS,30)},
    }

@app.get("/")
def root(): return {"ok": True, "msg": "EMS backend running"}

@app.on_event("startup")
async def on_start():
    lat = float(os.getenv("LAT", "48.87"))
    lon = float(os.getenv("LON", "12.65"))
    base_limit_kw = float(os.getenv("BASE_LIMIT_KW", "11"))
    asyncio.create_task(control_loop(app, lat, lon, base_limit_kw))

if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
