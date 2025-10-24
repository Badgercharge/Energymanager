# backend/main.py
import os
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from ocpp_cs import (
    CentralSystem,
    KNOWN_CP_IDS,
    cp_status,
    cp_registry,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("uvicorn")

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_origins(val: str) -> List[str]:
    if not val:
        return ["*"]
    return [o.strip() for o in val.split(",") if o.strip()]

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
ALLOWED_ORIGINS = parse_origins(FRONTEND_ORIGIN)

app = FastAPI(title="HomeCharger Backend", version="2.0.0")

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

# Standort / Preis / ECO
LAT = float(os.getenv("LAT", "48.83"))
LON = float(os.getenv("LON", "12.86"))
PRICE_API_URL = os.getenv("PRICE_API_URL", "https://api.awattar.de/v1/marketdata")

MIN_KW = float(os.getenv("MIN_KW", "3.7"))
MAX_KW = float(os.getenv("MAX_KW", "11.0"))

ECO = {
    "sunny_kw": float(os.getenv("SUNNY_KW", f"{MAX_KW}")),
    "cloudy_kw": float(os.getenv("CLOUDY_KW", f"{MIN_KW}")),
    "updated_at": now_iso(),
}

BOOST: Dict[str, Dict[str, Any]] = {}
def _boost_defaults() -> Dict[str, Any]:
    return {"enabled": False, "target_soc": 80, "by_time": None, "mode": "eco"}

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
# Health / Debug
# -----------------------------
@app.get("/")
async def root():
    return JSONResponse({"message": "HomeCharger Backend up. See /api/points and /ocpp/{cp_id}."})

@app.head("/")
async def root_head():
    return Response(status_code=200)

@app.get("/health")
async def health():
    return {"status": "ok", "as_of": now_iso()}

@app.head("/health")
async def health_head():
    return Response(status_code=200)

@app.get("/debug/routes")
def debug_routes():
    routes = sorted([r.path for r in app.router.routes])
    return routes

# -----------------------------
# API: Config / Price / Weather
# -----------------------------
@app.get("/api/config/eco")
async def api_config_eco_get():
    return ECO

@app.post("/api/config/eco")
async def api_config_eco_set(payload: Dict[str, Any]):
    if "sunny_kw" in payload:
        ECO["sunny_kw"] = float(payload["sunny_kw"])
    if "cloudy_kw" in payload:
        ECO["cloudy_kw"] = float(payload["cloudy_kw"])
    ECO["updated_at"] = now_iso()
    return ECO

@app.get("/api/price")
async def api_price():
    async with httpx.AsyncClient() as client:
        return await fetch_awattar_stats(client)

@app.get("/api/weather")
async def api_weather():
    async with httpx.AsyncClient() as client:
        return await fetch_open_meteo(client, LAT, LON)

# -----------------------------
# API: Points / Stats / Boost
# -----------------------------
@app.get("/api/points")
async def api_points():
    out = []
    for cid, st in cp_status.items():
        sess = st.get("session") or {}
        status_raw = (st.get("status") or "unknown")
        status_label = STATUS_LABELS.get(status_raw, status_raw)

        item = {
            "id": cid,
            "status": status_raw,
            "status_label": status_label,
            "last_seen": st.get("last_seen"),
            "vendor": st.get("vendor"),
            "model": st.get("model"),
            # Aliase + Defaultwerte
            "power_kw": float(st.get("power_kw") or 0.0),
            "current_kw": float(st.get("power_kw") or 0.0),
            "target_kw": st.get("target_kw"),
            "soc": st.get("soc"),
            "tx_active": bool(st.get("tx_active") or False),
            "transaction_active": bool(st.get("tx_active") or False),
            "energy_kwh_session": float(st.get("energy_kwh_session") or 0.0),
            "energy_kwh": float(st.get("energy_kwh_session") or 0.0),
            "session": {
                "start": sess.get("start"),
                "end": sess.get("end"),
                "est_end": sess.get("est_end"),
            },
            "boost": BOOST.get(cid) or _boost_defaults(),
        }
        out.append(item)
    return out

@app.get("/api/stats")
async def api_stats():
    total = len(cp_status)
    charging = sum(1 for s in cp_status.values() if s.get("status") == "charging")
    return {"total": total, "charging": charging, "as_of": now_iso()}

@app.get("/api/points/{cp_id}/boost")
async def api_boost_get(cp_id: str):
    return BOOST.get(cp_id) or _boost_defaults()

@app.post("/api/points/{cp_id}/boost")
async def api_boost_set(cp_id: str, payload: Dict[str, Any]):
    conf = BOOST.get(cp_id) or _boost_defaults()
    if "enabled" in payload:
        conf["enabled"] = bool(payload["enabled"])
    if "target_soc" in payload:
        try:
            conf["target_soc"] = max(1, min(100, int(payload["target_soc"])))
        except Exception:
            pass
    if "by_time" in payload:
        val = str(payload["by_time"]).strip() if payload["by_time"] is not None else None
        conf["by_time"] = val
    if "mode" in payload:
        mode = str(payload["mode"]).lower()
        if mode in ("eco", "price"):
            conf["mode"] = mode

    BOOST[cp_id] = conf
    st = cp_status.get(cp_id) or {"id": cp_id}
    st["boost"] = conf
    cp_status[cp_id] = st

    cp = cp_registry.get(cp_id)
    if conf["enabled"]:
        # Boost sofort auf 11 kW setzen (env override möglich)
        target_kw = float(os.getenv("BOOST_KW", "11.0"))
        st["target_kw"] = target_kw
        cp_status[cp_id] = st
        if cp is not None:
            await cp.push_limit_kw(target_kw)
    else:
        if cp is not None:
            await cp.clear_profile()
    return conf

# -----------------------------
# OCPP WebSocket Routen
# -----------------------------
async def _accept_ocpp(websocket: WebSocket, cp_id: str):
    # optional Whitelist
    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        await websocket.close(code=1008)
        log.info("connection rejected (unknown cp_id=%s)", cp_id)
        return

    # OCPP 1.6 Subprotocol
    await websocket.accept(subprotocol="ocpp1.6")

    cp = CentralSystem(cp_id, websocket)
    cp_registry[cp_id] = cp
    st = cp_status.get(cp_id) or {"id": cp_id}
    st["status"] = st.get("status") or "unknown"
    st["last_seen"] = now_iso()
    cp_status[cp_id] = st

    log.info("OCPP connect (ASGI): %s", cp_id)
    try:
        await cp.start()  # blockiert bis Disconnect
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error("OCPP ASGI session error (%s): %s", cp_id, e, exc_info=True)
    finally:
        cp_registry.pop(cp_id, None)
        log.info("OCPP disconnect (ASGI): %s", cp_id)

@app.websocket("/ocpp/{cp_id}")
async def ocpp_ws(websocket: WebSocket, cp_id: str):
    await _accept_ocpp(websocket, cp_id)

@app.websocket("/ocpp/{cp_id}/{tail:path}")
async def ocpp_ws_tail(websocket: WebSocket, cp_id: str, tail: str):
    # Einige Boxen hängen die CP-ID doppelt an; wir ignorieren tail
    await _accept_ocpp(websocket, cp_id)
