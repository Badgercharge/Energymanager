# backend/main.py
import os
import logging
from typing import List, Optional
from datetime import datetime, timezone, timedelta

import math
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import httpx

# OCPP-Server-Logik
from ocpp_cs import CentralSystem, extract_cp_id_from_path, KNOWN_CP_IDS, cp_status, cp_registry

# -----------------------------------------------------------------------------
# Logging / CORS
# -----------------------------------------------------------------------------
log = logging.getLogger("uvicorn")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

def parse_origins(val: str) -> List[str]:
    if not val:
        return ["*"]
    return [o.strip() for o in val.split(",") if o.strip()]

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
ALLOWED_ORIGINS = parse_origins(FRONTEND_ORIGIN)

app = FastAPI(title="HomeCharger Backend", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static NICHT auf "/" mounten, damit /api/* nicht überschrieben wird
if os.path.isdir("static"):
    app.mount("/app", StaticFiles(directory="static", html=True), name="static")

# -----------------------------------------------------------------------------
# In-Memory Config (Eco-Settings etc.)
# -----------------------------------------------------------------------------
# Defaults: nur SUNNY_KW und CLOUDY_KW steuerbar (3.7–11 kW Grenzen)
ECO = {
    "sunny_kw": float(os.getenv("SUNNY_KW", "11.0")),
    "cloudy_kw": float(os.getenv("CLOUDY_KW", "3.7")),
    "updated_at": datetime.now(timezone.utc).isoformat(),
}
MIN_KW = 3.7
MAX_KW = 11.0

# -----------------------------------------------------------------------------
# Health/Debug
# -----------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.head("/health")
async def health_head():
    return Response(status_code=200)

@app.get("/debug/routes")
def debug_routes():
    return sorted([r.path for r in app.router.routes])

@app.get("/")
async def root():
    return JSONResponse({"message": "HomeCharger Backend up. See /api/points and /ocpp/{cp_id}."})

@app.head("/")
async def root_head():
    return Response(status_code=200)

# -----------------------------------------------------------------------------
# API: OCPP-Status
# -----------------------------------------------------------------------------
@app.get("/api/points")
async def api_points():
    return list(cp_status.values())

# Optionaler Alias, falls dein Frontend mal /api/point ruft
@app.get("/api/point")
async def api_point():
    items = list(cp_status.values())
    return items[0] if items else {}

# -----------------------------------------------------------------------------
# API: Eco-Settings (nur sunny_kw, cloudy_kw)
# -----------------------------------------------------------------------------
@app.get("/api/eco")
async def get_eco():
    return ECO

# Kompatibilitäts-Alias: /api/settings führt auf dasselbe
@app.get("/api/settings")
async def get_settings():
    return ECO

@app.post("/api/eco")
async def set_eco(payload: dict):
    try:
        sunny = float(payload.get("sunny_kw", ECO["sunny_kw"]))
        cloudy = float(payload.get("cloudy_kw", ECO["cloudy_kw"]))
        # Grenzen erzwingen
        sunny = max(MIN_KW, min(MAX_KW, sunny))
        cloudy = max(MIN_KW, min(MAX_KW, cloudy))
        ECO.update({
            "sunny_kw": sunny,
            "cloudy_kw": cloudy,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return ECO
    except Exception as e:
        log.exception("set_eco error: %s", e)
        return JSONResponse({"error": "invalid payload"}, status_code=400)

@app.post("/api/settings")
async def set_settings(payload: dict):
    return await set_eco(payload)

# -----------------------------------------------------------------------------
# API: Preis (aWATTar stündlich, ohne API-Key)
#   PRICE_API_URL kann überschrieben werden; Default aWATTar DE.
#   Antwort: { as_of, current_ct_per_kwh, median_ct_per_kwh, below_or_equal_median }
# -----------------------------------------------------------------------------
PRICE_API_URL = os.getenv("PRICE_API_URL", "https://api.awattar.de/v1/marketdata")

async def fetch_prices_awattar(url: str) -> list[dict]:
    # aWATTar liefert Felder: start_timestamp (ms), end_timestamp (ms), marketprice (€/MWh)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    items = data.get("data", data)  # manche Proxies geben data direkt zurück
    out = []
    for it in items:
        start_ms = it.get("start_timestamp") or it.get("start") or it.get("startTime")
        price_eur_per_mwh = it.get("marketprice") or it.get("price")
        if start_ms is None or price_eur_per_mwh is None:
            continue
        # €/MWh -> ct/kWh: (€/MWh / 1000) * 100 = €/kWh*100ct/€ = /10
        ct_per_kwh = float(price_eur_per_mwh) / 10.0
        out.append({
            "start": int(start_ms),
            "ct_per_kwh": ct_per_kwh,
        })
    # nach Start sortieren
    out.sort(key=lambda x: x["start"])
    return out

def pick_current_price(items: list[dict], now_ms: int) -> Optional[float]:
    # aWATTar liefert stündliche Blöcke; wir nehmen den Block, dessen Start <= now < next_start
    for idx, it in enumerate(items):
        start = it["start"]
        end = items[idx + 1]["start"] if idx + 1 < len(items) else start + 3600_000
        if start <= now_ms < end:
            return it["ct_per_kwh"]
    return items[-1]["ct_per_kwh"] if items else None

@app.get("/api/price")
async def api_price():
    try:
        items = await fetch_prices_awattar(PRICE_API_URL)
        if not items:
            return {
                "as_of": datetime.now(timezone.utc).isoformat(),
                "current_ct_per_kwh": None,
                "median_ct_per_kwh": None,
                "below_or_equal_median": None,
            }
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        current = pick_current_price(items, now_ms)
        vals = [x["ct_per_kwh"] for x in items]
        median = sorted(vals)[len(vals)//2] if vals else None
        below = None
        if current is not None and median is not None:
            below = current <= median
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "current_ct_per_kwh": current,
            "median_ct_per_kwh": median,
            "below_or_equal_median": below,
        }
    except httpx.HTTPStatusError as e:
        log.warning("price upstream %s: %s", e.response.status_code, e)
        return JSONResponse({"error": f"upstream {e.response.status_code}"}, status_code=502)
    except Exception as e:
        log.exception("price error: %s", e)
        return JSONResponse({"error": "price error"}, status_code=500)

# -----------------------------------------------------------------------------
# API: Wetter (Open-Meteo, ohne Key)
#   Per ENV konfigurierbar: LAT, LON. Default = Radldorf (ca.).
#   Antwort: { as_of, cloud_cover_percent, ghi_w_m2, summary }
# -----------------------------------------------------------------------------
LAT = float(os.getenv("LAT", "48.83"))
LON = float(os.getenv("LON", "12.86"))

OPEN_METEO_URL = os.getenv(
    "WEATHER_API_URL",
    f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&current=cloud_cover,shortwave_radiation,temperature_2m,weather_code&timezone=auto"
)

@app.get("/api/weather")
async def api_weather():
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(OPEN_METEO_URL)
            resp.raise_for_status()
            data = resp.json()
        cur = data.get("current", {})
        cloud = cur.get("cloud_cover")
        ghi = cur.get("shortwave_radiation")
        summary = {
            "temp_c": cur.get("temperature_2m"),
            "weather_code": cur.get("weather_code"),
        }
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "cloud_cover_percent": cloud,
            "ghi_w_m2": ghi,
            "summary": summary,
        }
    except httpx.HTTPStatusError as e:
        log.warning("weather upstream %s: %s", e.response.status_code, e)
        return JSONResponse({"error": f"upstream {e.response.status_code}"}, status_code=502)
    except Exception as e:
        log.exception("weather error: %s", e)
        return JSONResponse({"error": "weather error"}, status_code=500)

# -----------------------------------------------------------------------------
# OCPP-WebSocket: akzeptiere /ocpp, /ocpp/{cp_id}, /ocpp/{cp_id}/{tail}
#   (tolerant gegen doppelte ID in der URL)
# -----------------------------------------------------------------------------
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

@app.websocket("/ocpp")
@app.websocket("/ocpp/{cp_id}")
@app.websocket("/ocpp/{cp_id}/{tail:path}")
async def ocpp_ws(ws: WebSocket, cp_id: str | None = None, tail: str | None = None):
    # Subprotocol versuchen
    try:
        await ws.accept(subprotocol="ocpp1.6")
    except Exception:
        await ws.accept()

    if not cp_id:
        cp_id = extract_cp_id_from_path("/ocpp")

    # Whitelist prüfen
    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        log.warning("Reject OCPP for unknown CP-ID: %s (tail=%s)", cp_id, tail)
        await ws.close(code=4000)
        return

    log.info("OCPP connect (ASGI): %s (tail=%s)", cp_id, tail)
    adapter = StarletteWSAdapter(ws)
    cp = CentralSystem(cp_id, adapter)
    cp_registry[cp_id] = cp
    try:
        await cp.start()  # blockiert bis Disconnect
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("OCPP ASGI session error (%s): %s", cp_id, e)
    finally:
        cp_registry.pop(cp_id, None)
        log.info("OCPP disconnect (ASGI): %s", cp_id)
