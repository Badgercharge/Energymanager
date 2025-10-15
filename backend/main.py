import asyncio, json, logging, os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from ocpp.v16 import ChargePoint as CP
from ocpp.v16.enums import Protocol
from ocpp.routing import Router
from ocpp.v16.enums import Action
from ocpp.v16 import call_result
from ocpp.transport import WebSocket as OcppWebSocket
from ocpp_cs import CentralSystem
from models import STATE
from scheduler import control_loop
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.cps = {}

@app.websocket("/ocpp/{cp_id}")
async def ocpp(ws: WebSocket, cp_id: str):
    await ws.accept(subprotocol=Protocol.ocpp1_6.value)
    ocpp_ws = OcppWebSocket(ws)
    cp = CentralSystem(cp_id, ocpp_ws)
    app.state.cps[cp_id] = cp
    try:
        await cp.start()
    except WebSocketDisconnect:
        pass
    finally:
        STATE.get(cp_id) and setattr(STATE[cp_id], "connected", False)
        app.state.cps.pop(cp_id, None)

@app.get("/api/points")
def list_points():
    return [vars(s) for s in STATE.values()]

@app.post("/api/points/{cp_id}/mode/{mode}")
def set_mode(cp_id: str, mode: str):
    assert mode in ["eco","max","off","schedule"]
    STATE[cp_id].mode = mode
    return {"ok": True}

@app.post("/api/points/{cp_id}/limit")
def set_limit(cp_id: str, kw: float):
    STATE[cp_id].target_kw = kw
    return {"ok": True}

@app.on_event("startup")
async def on_start():
    lat = float(os.getenv("LAT", "48.14"))
    lon = float(os.getenv("LON", "11.58"))
    base_limit_kw = float(os.getenv("BASE_LIMIT_KW", "11"))
    min_grid_kw = float(os.getenv("MIN_GRID_KW", "0.5"))
    asyncio.create_task(control_loop(app, lat, lon, base_limit_kw, min_grid_kw))
