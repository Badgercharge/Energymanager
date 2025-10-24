# backend/main.py
import os
import json
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from .ocpp_cs import CentralSystem, cp_status, cp_registry, KNOWN_CP_IDS

# -----------------------
# Logging + Live-Log-Puffer
# -----------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s:%(name)s:%(message)s",
)

LOG_BUFFER = deque(maxlen=500)

class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            })
        except Exception:
            pass

_buffer_handler = BufferHandler()
logging.getLogger().addHandler(_buffer_handler)
logging.getLogger("ocpp").addHandler(_buffer_handler)

log = logging.getLogger("backend")

# -----------------------
# FastAPI
# -----------------------
app = FastAPI(title="Home Charger EMS", version="1.0.0")

# CORS (erlaube Frontend auf Vercel oder alle)
ALLOW_ORIGINS = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------
# Utils
# -----------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

# -----------------------
# Root / Health
# -----------------------
@app.get("/")
def root():
    return {"ok": True, "app": "Home Charger EMS", "version": "1.0.0", "time": now_iso()}

# -----------------------
# Live Logs Endpoint
# -----------------------
@app.get("/api/logs")
def api_logs(limit: int = 200):
    limit = max(10, min(500, int(limit or 200)))
    return list(LOG_BUFFER)[-limit:]

# -----------------------
# OCPP WebSocket Endpoint
# -----------------------
class StarletteWSConn:
    """Adapter, der Starlette WebSocket der ocpp.ChargePoint-API anpasst (recv/send)."""
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._closed = False

    async def recv(self) -> str:
        msg = await self.ws.receive_text()
        return msg

    async def send(self, msg: str):
        await self.ws.send_text(msg)

    @property
    def closed(self) -> bool:
        return self._closed or self.ws.client_state.name != "CONNECTED"

    async def close(self):
        self._closed = True
        try:
            await self.ws.close()
        except Exception:
            pass

@app.websocket("/ocpp/{cp_id}")
async def ocpp_ws(websocket: WebSocket, cp_id: str):
    # Whitelist optional
    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        await websocket.close(code=4030)
        log.info("Reject WS for unknown CP-ID %s", cp_id)
        return

    # OCPP 1.6 Subprotocol akzeptieren
    await websocket.accept(subprotocol="ocpp1.6")
    log.info("OCPP connect (ASGI): %s", cp_id)
    conn = StarletteWSConn(websocket)
    cp = CentralSystem(cp_id, conn)
    cp_registry[cp_id] = cp

    # Init minimalen Status
    st = cp_status.get(cp_id) or {"id": cp_id}
    st.setdefault("status", "unknown")
    st["last_seen"] = now_iso()
    cp_status[cp_id] = st

    try:
        await cp.start()  # blockiert bis Disconnect
    except WebSocketDisconnect:
        log.info("%s: WebSocket disconnected", cp_id)
    except Exception as e:
        log.exception("%s: OCPP session error: %s", cp_id, e)
    finally:
        try:
            await conn.close()
        except Exception:
            pass
        cp_registry.pop(cp_id, None)
        st = cp_status.get(cp_id) or {"id": cp_id}
        st["status"] = "offline"
        st["last_seen"] = now_iso()
        cp_status[cp_id] = st
        log.info("%s: connection closed", cp_id)

# -----------------------
# API: Points
# -----------------------
@app.get("/api/points")
def api_points() -> List[Dict[str, Any]]:
    return list(cp_status.values())

@app.get("/api/points/{cp_id}")
def api_point(cp_id: str) -> Dict[str, Any]:
    return cp_status.get(cp_id, {"id": cp_id, "status": "unknown"})

@app.post("/api/points/{cp_id}/limit")
async def api_point_limit(cp_id: str, body: Dict[str, Any]):
    kw = float(body.get("kw", 0))
    kw = max(0.0, kw)
    cp = cp_registry.get(cp_id)
    if not cp:
        return JSONResponse({"ok": False, "error": "charger_offline"}, status_code=409)
    await cp.push_limit_kw(kw)
    return {"ok": True, "target_kw": kw}

@app.post("/api/points/{cp_id}/boost")
async def api_point_boost(cp_id: str, body: Optional[Dict[str, Any]] = None):
    # Standard: Boost = 11 kW
    kw = 11.0
    if body and "kw" in body:
        try:
            kw = float(body.get("kw"))
        except Exception:
            pass
    cp = cp_registry.get(cp_id)
    if not cp:
        return JSONResponse({"ok": False, "error": "charger_offline"}, status_code=409)
    await cp.push_limit_kw(kw)
    return {"ok": True, "boost_kw": kw}

# -----------------------
# API: Weather (Open-Meteo)
# -----------------------
@app.get("/api/weather")
async def api_weather():
    lat = os.getenv("LAT", "48.83")
    lon = os.getenv("LON", "12.86")
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=cloud_cover,shortwave_radiation,temperature_2m,weather_code&timezone=auto"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    cur = data.get("current", {})
    return {
        "as_of": data.get("current_units", {}),
        "cloud_cover": cur.get("cloud_cover"),
        "shortwave_radiation": cur.get("shortwave_radiation"),
        "temperature_2m": cur.get("temperature_2m"),
        "weather_code": cur.get("weather_code"),
    }

# -----------------------
# API: Price (aWATTar)
# -----------------------
@app.get("/api/price")
async def api_price():
    price_url = os.getenv("PRICE_API_URL", "https://api.awattar.de/v1/marketdata")
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(price_url)
        r.raise_for_status()
        rows = r.json().get("data", [])
    # ct/kWh berechnen (EUR/MWh -> ct/kWh = EUR/MWh / 10)
    slots = []
    for it in rows:
        start = int(it.get("start_timestamp"))
        end = int(it.get("end_timestamp"))
        eur_per_mwh = float(it.get("marketprice", 0.0))
        ct_per_kwh = eur_per_mwh / 10.0
        slots.append({"start": start, "end": end, "ct_per_kwh": ct_per_kwh})
    slots.sort(key=lambda x: x["start"])
    current = next((s for s in slots if s["start"] <= now_ms < s["end"]), None)
    # Median der nächsten 24h
    next24 = slots[:24] if len(slots) >= 24 else slots
    med = None
    if next24:
        vals = sorted(s["ct_per_kwh"] for s in next24)
        n = len(vals)
        med = vals[n // 2] if n % 2 == 1 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "current_ct_per_kwh": round(current["ct_per_kwh"], 3) if current else None,
        "median_ct_per_kwh": round(med, 3) if med is not None else None,
        "below_or_equal_median": (current and med is not None and current["ct_per_kwh"] <= med),
        "slots": slots,
    }

# -----------------------
# API: Simple stats
# -----------------------
@app.get("/api/stats")
def api_stats():
    return {
        "points": len(cp_status),
        "online": sum(1 for s in cp_status.values() if s.get("status") not in ("offline", "unknown")),
        "time": now_iso(),
        "version": "1.0.0",
    }

# -----------------------
# Optional: Debug routes listing
# -----------------------
@app.get("/debug/routes")
def debug_routes():
    out = []
    for r in app.router.routes:
        try:
            out.append({"path": r.path, "methods": list(getattr(r, "methods", []))})
        except Exception:
            pass
    return out
