# backend/main.py
import os
import json
import logging
from collections import deque
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from ocpp_cs import (
    CentralSystem,
    KNOWN_CP_IDS,
    cp_status,
    cp_registry,
)

# -----------------------------
# Logging + Log-Puffer (Live Logs)
# -----------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("uvicorn")

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


logging.getLogger("ocpp").addHandler(BufferHandler())
logging.getLogger("uvicorn.error").addHandler(BufferHandler())
logging.getLogger("uvicorn.access").addHandler(BufferHandler())

# -----------------------------
# App + CORS + Static
# -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_origins(val: str) -> List[str]:
    if not val:
        return ["*"]
    return [o.strip() for o in val.split(",") if o.strip()]


FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
ALLOWED_ORIGINS = parse_origins(FRONTEND_ORIGIN)

app = FastAPI(title="HomeCharger Backend", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static NICHT auf "/" mounten, um /api und /ocpp nicht zu überschreiben
if os.path.isdir("static"):
    app.mount("/app", StaticFiles(directory="static", html=True), name="static")

# -----------------------------
# Konfigurationen
# -----------------------------
LAT = float(os.getenv("LAT", "48.83"))
LON = float(os.getenv("LON", "12.86"))
PRICE_API_URL = os.getenv("PRICE_API_URL", "https://api.awattar.de/v1/marketdata")

MIN_KW = float(os.getenv("MIN_KW", "3.7"))
MAX_KW = float(os.getenv("MAX_KW", "11.0"))
BOOST_DEFAULT_KW = float(os.getenv("BOOST_KW", "11.0"))

ECO = {
    "sunny_kw": float(os.getenv("SUNNY_KW", f"{MAX_KW}")),
    "cloudy_kw": float(os.getenv("CLOUDY_KW", f"{MIN_KW}")),
    "updated_at": now_iso(),
}

BOOST: Dict[str, Dict[str, Any]] = {}
def _boost_defaults() -> Dict[str, Any]:
    return {"enabled": False, "kw": BOOST_DEFAULT_KW, "target_soc": None, "by_time": None, "mode": "price"}

STATUS_LABELS = {
    "unknown": "Unbekannt",
    "available": "Bereit",
    "preparing": "Vorbereitung",
    "charging": "Lädt",
    "suspended_ev": "Pausiert (EV)",
    "suspendedev": "Pausiert (EV)",
    "suspended_evse": "Pausiert (EVSE)",
    "suspendedevse": "Pausiert (EVSE)",
    "finishing": "Beenden",
    "faulted": "Fehler",
}

# -----------------------------
# Helpers: Price / Weather
# -----------------------------
async def fetch_awattar_current_ct_per_kwh(client: httpx.AsyncClient) -> Optional[float]:
    """
    A-WATTAR: marketprice in EUR/MWh -> ct/kWh = (EUR/MWh) / 10
    """
    try:
        r = await client.get(PRICE_API_URL, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("data", [])
        if not items:
            return None
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        current = None
        for it in items:
            if it.get("start_timestamp") <= now_ms < it.get("end_timestamp"):
                current = it
                break
        if not current:
            current = items[0]
        eur_per_mwh = float(current.get("marketprice"))
        return round(eur_per_mwh / 10.0, 3)
    except Exception as e:
        log.warning("price fetch error: %s", e)
        return None


async def fetch_awattar_stats(client: httpx.AsyncClient) -> Dict[str, Any]:
    out = {"as_of": now_iso(), "current_ct_per_kwh": None, "median_ct_per_kwh": None, "below_or_equal_median": None, "source": "awattar"}
    try:
        r = await client.get(PRICE_API_URL, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("data", [])
        if not items:
            return out
        prices_ct = [float(it["marketprice"]) / 10.0 for it in items if "marketprice" in it]
        if prices_ct:
            s = sorted(prices_ct)
            n = len(s)
            median = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2.0
            out["median_ct_per_kwh"] = round(median, 3)
        out["current_ct_per_kwh"] = await fetch_awattar_current_ct_per_kwh(client)
        if out["current_ct_per_kwh"] is not None and out["median_ct_per_kwh"] is not None:
            out["below_or_equal_median"] = out["current_ct_per_kwh"] <= out["median_ct_per_kwh"]
    except Exception as e:
        log.warning("price stats error: %s", e)
    return out


async def fetch_open_meteo(client: httpx.AsyncClient, lat: float, lon: float) -> Dict[str, Any]:
    out = {
        "as_of": now_iso(),
        "latitude": lat,
        "longitude": lon,
        "cloud_cover": None,
        "shortwave_radiation": None,
        "temperature_2m": None,
        "weather_code": None,
        "source": "open-meteo",
    }
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=cloud_cover,shortwave_radiation,temperature_2m,weather_code"
        "&timezone=auto"
    )
    try:
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        cur = data.get("current", {})
        out["cloud_cover"] = cur.get("cloud_cover")
        out["shortwave_radiation"] = cur.get("shortwave_radiation")
        out["temperature_2m"] = cur.get("temperature_2m")
        out["weather_code"] = cur.get("weather_code")
    except Exception as e:
        log.warning("weather fetch error: %s", e)
    return out

# -----------------------------
# API: Basics
# -----------------------------
@app.get("/")
def root():
    return {"message": "HomeCharger Backend up. See /api/points and /ocpp/{cp_id}."}


@app.get("/health")
def health():
    return {"ok": True, "ts": now_iso()}


@app.get("/debug/routes")
def debug_routes():
    return [route.path for route in app.router.routes]

# -----------------------------
# API: Logs
# -----------------------------
@app.get("/api/logs")
async def api_logs(limit: int = 200):
    data = list(LOG_BUFFER)[-int(limit):]
    return {"items": data, "count": len(data)}

# -----------------------------
# API: Price / Weather / Stats
# -----------------------------
@app.get("/api/price")
async def api_price():
    async with httpx.AsyncClient() as client:
        return await fetch_awattar_stats(client)


@app.get("/api/weather")
async def api_weather():
    async with httpx.AsyncClient() as client:
        return await fetch_open_meteo(client, LAT, LON)


@app.get("/api/stats")
async def api_stats():
    # Klein zusammengefasst
    points = list(cp_status.values())
    total_power = round(sum(p.get("power_kw") or 0.0 for p in points), 3) if points else 0.0
    charging = sum(1 for p in points if (p.get("status") == "charging"))
    return {"points": len(points), "charging": charging, "total_power_kw": total_power, "ts": now_iso()}

# -----------------------------
# API: ECO-Config (einfach)
# -----------------------------
@app.get("/api/config/eco")
async def get_eco():
    return ECO


@app.post("/api/config/eco")
async def set_eco(cfg: Dict[str, Any]):
    if "sunny_kw" in cfg:
        ECO["sunny_kw"] = float(cfg["sunny_kw"])
    if "cloudy_kw" in cfg:
        ECO["cloudy_kw"] = float(cfg["cloudy_kw"])
    ECO["updated_at"] = now_iso()
    return ECO

# -----------------------------
# API: Punkte / Boost
# -----------------------------
def map_point_view(st: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": st.get("id"),
        "status": st.get("status"),
        "status_label": STATUS_LABELS.get(st.get("status") or "unknown", st.get("status") or "unknown"),
        "vendor": st.get("vendor"),
        "model": st.get("model"),
        "power_kw": st.get("power_kw"),
        "target_kw": st.get("target_kw"),
        "soc": st.get("soc"),
        "energy_kwh_session": st.get("energy_kwh_session"),
        "tx_active": st.get("tx_active") or False,
        "last_seen": st.get("last_seen"),
        "session": st.get("session"),
    }


@app.get("/api/points")
async def api_points():
    return [map_point_view(v) for v in cp_status.values()]


@app.get("/api/points/{cp_id}")
async def api_point(cp_id: str):
    st = cp_status.get(cp_id)
    if not st:
        return JSONResponse(map_point_view({"id": cp_id, "status": "unknown"}))
    return map_point_view(st)


@app.get("/api/points/{cp_id}/boost")
async def get_boost(cp_id: str):
    return BOOST.get(cp_id, _boost_defaults())


@app.post("/api/points/{cp_id}/boost")
async def set_boost(cp_id: str, body: Dict[str, Any]):
    cfg = BOOST.get(cp_id, _boost_defaults())
    enabled = bool(body.get("enabled", cfg["enabled"]))
    kw = float(body.get("kw", cfg["kw"]))
    cfg.update({"enabled": enabled, "kw": kw})
    BOOST[cp_id] = cfg

    cp = cp_registry.get(cp_id)
    if cp:
        if enabled:
            # Boost sofort mit gewünschter kW (Standard 11.0)
            await cp.push_limit_kw(max(MIN_KW, min(kw, MAX_KW)))
        else:
            await cp.clear_profile()
    return cfg

# -----------------------------
# OCPP WebSocket Routen
# -----------------------------
class FastAPIConnection:
    """Adapter für ocpp ChargePoint, nutzt FastAPI WebSocket."""
    def __init__(self, websocket: WebSocket):
        self.ws = websocket

    async def recv(self) -> str:
        msg = await self.ws.receive_text()
        return msg

    async def send(self, message: str):
        await self.ws.send_text(message)

    async def close(self):
        await self.ws.close()


async def _ocpp_session(websocket: WebSocket, cp_id: str):
    # Subprotocol prüfen und akzeptieren
    subp = websocket.headers.get("sec-websocket-protocol", "")
    if "ocpp1.6" not in subp:
        # Viele Boxen erwarten Echo des Subprotocols
        await websocket.accept(subprotocol="ocpp1.6")
    else:
        await websocket.accept(subprotocol="ocpp1.6")

    # Whitelist (falls gesetzt)
    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        await websocket.close(code=4403)
        return

    connection = FastAPIConnection(websocket)
    cp = CentralSystem(cp_id, connection)
    cp_registry[cp_id] = cp

    # Sichtbarkeit im Status herstellen
    st = cp_status.get(cp_id) or {"id": cp_id}
    st["status"] = st.get("status") or "available"
    st["last_seen"] = now_iso()
    cp_status[cp_id] = st

    log.info("OCPP connect (ASGI): %s", cp_id)
    try:
        await cp.start()  # blockiert bis Disconnect
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("OCPP ASGI session error (%s): %s", cp_id, e)
    finally:
        cp_registry.pop(cp_id, None)
        st = cp_status.get(cp_id) or {"id": cp_id}
        st["last_seen"] = now_iso()
        cp_status[cp_id] = st
        log.info("OCPP disconnect (ASGI): %s", cp_id)


@app.websocket("/ocpp/{cp_id}")
async def ocpp_ws_cp(websocket: WebSocket, cp_id: str):
    await _ocpp_session(websocket, cp_id)


@app.websocket("/ocpp/{cp_id}/{tail:path}")
async def ocpp_ws_cp_tail(websocket: WebSocket, cp_id: str, tail: str):
    # Für KEBA, die manchmal die CP-ID doppelt anhängen
    await _ocpp_session(websocket, cp_id)


# Optionaler Fallback ohne cp_id -> 400
@app.websocket("/ocpp")
async def ocpp_ws_noid(websocket: WebSocket):
    await websocket.close(code=4400, reason="cp_id required in path /ocpp/{cp_id}")
