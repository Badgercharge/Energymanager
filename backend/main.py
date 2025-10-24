# backend/main.py
import os
import math
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# OCPP-Server-Klasse und Status-Registry
from ocpp_cs import (
    CentralSystem,
    extract_cp_id_from_path,
    KNOWN_CP_IDS,
    cp_status,
    cp_registry,
)

# -----------------------------------------------------------------------------
# Logging / CORS
# -----------------------------------------------------------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("uvicorn")

def parse_origins(val: str) -> List[str]:
    if not val:
        return ["*"]
    return [o.strip() for o in val.split(",") if o.strip()]

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
ALLOWED_ORIGINS = parse_origins(FRONTEND_ORIGIN)

app = FastAPI(title="HomeCharger Backend", version="1.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WICHTIG: Static NICHT auf "/" mounten, damit /api/* und /ocpp nicht überschrieben werden
if os.path.isdir("static"):
    app.mount("/app", StaticFiles(directory="static", html=True), name="static")

# -----------------------------------------------------------------------------
# In-Memory Config / Defaults
# -----------------------------------------------------------------------------
# Standort (Radldorf als Default)
LAT = float(os.getenv("LAT", "48.83"))
LON = float(os.getenv("LON", "12.86"))

# Preis-API (A-WATTAR DE ist ohne API-Key)
PRICE_API_URL = os.getenv("PRICE_API_URL", "https://api.awattar.de/v1/marketdata")

# Eco-Einstellungen (vereinfacht: sunny_kw, cloudy_kw)
ECO = {
    "sunny_kw": float(os.getenv("SUNNY_KW", "11.0")),
    "cloudy_kw": float(os.getenv("CLOUDY_KW", "3.7")),
    "updated_at": datetime.now(timezone.utc).isoformat(),
}
MIN_KW = 3.7
MAX_KW = 11.0

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

async def fetch_awattar_current_ct_per_kwh(client: httpx.AsyncClient) -> Optional[float]:
    """
    Holt die aktuellen stündlichen Preise von aWATTar und gibt den ct/kWh-Wert
    für das aktuelle Zeitfenster zurück. aWATTar liefert marketprice in EUR/MWh.
    Umrechnung: ct/kWh = (EUR/MWh) / 10.
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
            # Fallback: nächstes/letztes Zeitfenster
            current = items[0]
        eur_per_mwh = float(current.get("marketprice"))
        ct_per_kwh = eur_per_mwh / 10.0
        return round(ct_per_kwh, 3)
    except Exception as e:
        log.warning("price fetch error: %s", e)
        return None

async def fetch_awattar_stats(client: httpx.AsyncClient) -> Dict[str, Any]:
    """
    Gibt median_ct_per_kwh über den gelieferten Zeitraum zurück und markiert,
    ob der aktuelle Preis <= Median liegt.
    """
    out = {
        "as_of": now_iso(),
        "current_ct_per_kwh": None,
        "median_ct_per_kwh": None,
        "below_or_equal_median": None,
        "source": "awattar",
    }
    try:
        r = await client.get(PRICE_API_URL, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("data", [])
        if not items:
            return out
        prices_ct = [float(it["marketprice"]) / 10.0 for it in items if "marketprice" in it]
        if prices_ct:
            prices_ct_sorted = sorted(prices_ct)
            n = len(prices_ct_sorted)
            if n % 2 == 1:
                median = prices_ct_sorted[n // 2]
            else:
                median = (prices_ct_sorted[n // 2 - 1] + prices_ct_sorted[n // 2]) / 2.0
            out["median_ct_per_kwh"] = round(median, 3)
        # Current
        out["current_ct_per_kwh"] = await fetch_awattar_current_ct_per_kwh(client)
        if out["current_ct_per_kwh"] is not None and out["median_ct_per_kwh"] is not None:
            out["below_or_equal_median"] = out["current_ct_per_kwh"] <= out["median_ct_per_kwh"]
    except Exception as e:
        log.warning("price stats error: %s", e)
    return out

async def fetch_open_meteo(client: httpx.AsyncClient, lat: float, lon: float) -> Dict[str, Any]:
    """
    Holt einfache Wetterdaten (aktueller Wolkenanteil, kurzwellige Strahlung, Temperatur).
    """
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

# -----------------------------------------------------------------------------
# Health / Debug / Root
# -----------------------------------------------------------------------------
@app.get("/")
async def root():
    return JSONResponse({"message": "HomeCharger Backend up. See /api/points and /ocpp/{cp_id}."})

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

# -----------------------------------------------------------------------------
# API: Points / Stats / Eco / Price / Weather
# -----------------------------------------------------------------------------
@app.get("/api/points")
async def api_points():
    """
    Liefert alle bekannten Ladepunkte mit Status (aus cp_status).
    """
    # flache Kopie für stabile Ausgabe
    out = []
    for cid, st in cp_status.items():
        out.append(st)
    return out

@app.get("/api/stats")
async def api_stats():
    """
    Kleiner Überblick über Sessions/Leistung.
    """
    sessions = []
    for st in cp_status.values():
        sess = st.get("session") or {}
        sessions.append({
            "id": st.get("id"),
            "status": st.get("status"),
            "start": sess.get("start"),
            "end": sess.get("end"),
            "energy_kwh": st.get("energy_kwh_session", 0.0),
            "power_kw": st.get("power_kw", 0.0),
            "soc": st.get("soc"),
        })
    return {"as_of": now_iso(), "sessions": sessions}

@app.get("/api/eco")
async def api_eco_get():
    return ECO

@app.post("/api/eco")
async def api_eco_set(payload: Dict[str, Any]):
    """
    Erwartet z. B. {"sunny_kw": 11.0, "cloudy_kw": 3.7}
    """
    sunny = payload.get("sunny_kw", ECO["sunny_kw"])
    cloudy = payload.get("cloudy_kw", ECO["cloudy_kw"])
    try:
        sunny = float(sunny)
        cloudy = float(cloudy)
    except Exception:
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    ECO["sunny_kw"] = clamp(sunny, MIN_KW, MAX_KW)
    ECO["cloudy_kw"] = clamp(cloudy, MIN_KW, MAX_KW)
    ECO["updated_at"] = now_iso()
    return ECO

# Alias für bestehendes Frontend (vermeidet 404)
@app.get("/api/config/eco")
async def api_config_eco_alias():
    return ECO

@app.get("/api/price")
async def api_price():
    async with httpx.AsyncClient() as client:
        stats = await fetch_awattar_stats(client)
    return stats

@app.get("/api/weather")
async def api_weather():
    async with httpx.AsyncClient() as client:
        weather = await fetch_open_meteo(client, LAT, LON)
    return weather

# -----------------------------------------------------------------------------
# OCPP WebSocket Endpoints (1.6 JSON, Subprotocol: 'ocpp1.6')
# -----------------------------------------------------------------------------
class FastAPIWebSocketWrapper:
    """
    Adaptiert fastapi.WebSocket auf das Interface, das die ocpp-Bibliothek erwartet:
    - send(str)
    - recv() -> str
    und stellt subprotocol bereit.
    """
    def __init__(self, ws: WebSocket, subprotocol: str = "ocpp1.6"):
        self._ws = ws
        self.subprotocol = subprotocol

    async def send(self, message: str):
        await self._ws.send_text(message)

    async def recv(self) -> str:
        return await self._ws.receive_text()

async def _serve_ocpp(websocket: WebSocket, path_override: Optional[str] = None):
    """
    Gemeinsame Handler-Logik für alle /ocpp*-Routen.
    """
    # Subprotocol aushandeln (zwingend für viele Boxen)
    await websocket.accept(subprotocol="ocpp1.6")

    # CP-ID aus dem URL-Pfad extrahieren (robust gegen doppelte IDs)
    path = path_override or websocket.url.path
    cp_id = extract_cp_id_from_path(path)
    if not cp_id or cp_id.lower() == "unknown":
        # versuche Header-basiert (einige Boxen senden sie so)
        cp_id = (websocket.headers.get("Sec-WebSocket-Protocol") or "").replace("ocpp1.6", "").strip() or "unknown"

    # Whitelist prüfen (falls gesetzt)
    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        log.info("connection rejected (403 Forbidden) for cp_id=%s", cp_id)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    log.info("OCPP connect (ASGI): %s (tail=None)", cp_id)
    connection = FastAPIWebSocketWrapper(websocket, subprotocol="ocpp1.6")
    cp = CentralSystem(cp_id, connection)
    cp_registry[cp_id] = cp  # registrieren

    try:
        await cp.start()  # blockiert, bis die Verbindung beendet wird
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error("OCPP ASGI session error (%s): %s", cp_id, e, exc_info=True)
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        log.info("OCPP disconnect (ASGI): %s", cp_id)
        # Optional: aufräumen, aber Status behalten
        # cp_registry.pop(cp_id, None)

@app.websocket("/ocpp")
async def ocpp_ws_root(websocket: WebSocket):
    await _serve_ocpp(websocket)

@app.websocket("/ocpp/{cp_id}")
async def ocpp_ws_with_id(websocket: WebSocket, cp_id: str):
    # Wir nutzen den echten Pfad, um doppelte IDs zu erkennen (…/ocpp/123/123)
    await _serve_ocpp(websocket)

@app.websocket("/ocpp/{cp_id}/{tail:path}")
async def ocpp_ws_with_tail(websocket: WebSocket, cp_id: str, tail: str):
    await _serve_ocpp(websocket)

# -----------------------------------------------------------------------------
# Ende
# -----------------------------------------------------------------------------
