# backend/main.py
import os
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# OCPP-Server-Klasse und Status-Registry (aus deiner ocpp_cs.py)
from ocpp_cs import (
    CentralSystem,
    extract_cp_id_from_path,
    KNOWN_CP_IDS,
    cp_status,
    cp_registry,
)

# -----------------------------------------------------------------------------
# Logging / App
# -----------------------------------------------------------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("uvicorn")

def parse_origins(val: str) -> List[str]:
    if not val:
        return ["*"]
    return [o.strip() for o in val.split(",") if o.strip()]

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
ALLOWED_ORIGINS = parse_origins(FRONTEND_ORIGIN)

app = FastAPI(title="HomeCharger Backend", version="1.6.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Wichtig: Static NICHT auf "/" mounten, damit /api/* und /ocpp nicht überschrieben werden
if os.path.isdir("static"):
    app.mount("/app", StaticFiles(directory="static", html=True), name="static")

# -----------------------------------------------------------------------------
# Standort / Konfiguration
# -----------------------------------------------------------------------------
LAT = float(os.getenv("LAT", "48.83"))   # Radldorf
LON = float(os.getenv("LON", "12.86"))

# Preis: A-WATTAR Deutschland (ohne API-Key)
PRICE_API_URL = os.getenv("PRICE_API_URL", "https://api.awattar.de/v1/marketdata")

# Eco-Settings (vereinfacht: sunny_kw, cloudy_kw)
MIN_KW = 3.7
MAX_KW = 11.0
ECO = {
    "sunny_kw": float(os.getenv("SUNNY_KW", f"{MAX_KW}")),
    "cloudy_kw": float(os.getenv("CLOUDY_KW", f"{MIN_KW}")),
    "updated_at": datetime.now(timezone.utc).isoformat(),
}

# Boost-Konfiguration (in-memory, pro CP)
# Struktur: {"enabled": bool, "target_soc": int, "by_time": "HH:MM", "mode": "eco"|"price"}
BOOST: Dict[str, Dict[str, Any]] = {}

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

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

async def fetch_awattar_current_ct_per_kwh(client: httpx.AsyncClient) -> Optional[float]:
    """
    A-WATTAR liefert marketprice in EUR/MWh.
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
            current = items[0]
        eur_per_mwh = float(current.get("marketprice"))
        return round(eur_per_mwh / 10.0, 3)
    except Exception as e:
        log.warning("price fetch error: %s", e)
        return None

async def fetch_awattar_stats(client: httpx.AsyncClient) -> Dict[str, Any]:
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

# -----------------------------------------------------------------------------
# Health / Debug / Root
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# API: Points / Stats / Eco / Price / Weather
# -----------------------------------------------------------------------------
@app.get("/api/points")
async def api_points():
    """
    Liefert Status der Ladepunkte inkl. UI‑Aliase und stabilen Default‑Werten,
    damit das Frontend nie null/undefined anzeigen muss.
    """
    out = []
    for cid, st in cp_status.items():
        sess = st.get("session") or {}
        status_raw = (st.get("status") or "Unknown")
        status_key = str(status_raw).replace(" ", "_").lower()
        status_label = STATUS_LABELS.get(status_key, "Unbekannt")

        power_kw = st.get("power_kw")
        if power_kw is None:
            power_kw = 0.0
        energy_kwh_session = st.get("energy_kwh_session")
        if energy_kwh_session is None:
            energy_kwh_session = 0.0

        out.append({
            "id": cid,
            "status": status_raw,
            "status_label": status_label,

            # Leistung
            "power_kw": round(float(power_kw), 3),
            "current_kw": round(float(power_kw), 3),  # UI-Alias
            "target_kw": (st.get("target_kw") if st.get("target_kw") is not None else 0.0),
            "mode": st.get("mode"),

            # Transaktion
            "tx_active": bool(st.get("tx_active")),
            "transaction_active": bool(st.get("tx_active")),  # UI-Alias

            # Session
            "energy_kwh_session": round(float(energy_kwh_session), 3),
            "energy_kwh": round(float(energy_kwh_session), 3),  # UI-Alias
            "session_start": (sess.get("start") or None),
            "session_end": (sess.get("end") or None),
            "est_end": (sess.get("est_end") or None),

            # Boost-Zustand (falls gesetzt)
            "boost": st.get("boost"),

            # Sonstiges
            "soc": st.get("soc"),
            "last_seen": st.get("last_seen"),
            "model": st.get("model"),
            "vendor": st.get("vendor"),
        })
    return out

@app.get("/api/stats")
async def api_stats():
    """
    Kleiner Überblick über Sessions/Leistung (aggregiert aus cp_status).
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

# Legacy-Alias, um 404 im Frontend zu vermeiden
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
# API: Boost (neu) – GET/POST pro Ladepunkt
# -----------------------------------------------------------------------------
def _boost_defaults() -> Dict[str, Any]:
    return {"enabled": False, "target_soc": 100, "by_time": "07:00", "mode": "eco"}

@app.get("/api/points/{cp_id}/boost")
async def api_boost_get(cp_id: str):
    conf = BOOST.get(cp_id) or _boost_defaults()
    # Spiegel in cp_status für Frontend
    st = cp_status.get(cp_id)
    if st is not None:
        st["boost"] = conf
        cp_status[cp_id] = st
    return conf

@app.post("/api/points/{cp_id}/boost")
async def api_boost_set(cp_id: str, payload: Dict[str, Any]):
    conf = BOOST.get(cp_id) or _boost_defaults()
    if "enabled" in payload:
        conf["enabled"] = bool(payload["enabled"])
    if "target_soc" in payload:
        try:
            conf["target_soc"] = max(1, min(100, int(payload["target_soc"])))
        except Exception:
            return JSONResponse({"error": "target_soc must be int 1..100"}, status_code=400)
    if "by_time" in payload:
        val = str(payload["by_time"]).strip()
        # naive Prüfung HH:MM
        try:
            hh, mm = val.split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError()
            conf["by_time"] = f"{h:02d}:{m:02d}"
        except Exception:
            return JSONResponse({"error": "by_time must be 'HH:MM'"}, status_code=400)
    if "mode" in payload:
        mode = str(payload["mode"]).lower()
        if mode not in ("eco", "price"):
            return JSONResponse({"error": "mode must be 'eco' or 'price'"}, status_code=400)
        conf["mode"] = mode

    BOOST[cp_id] = conf
    # Auch im Status ablegen, damit das Frontend es sofort sieht
    st = cp_status.get(cp_id) or {"id": cp_id}
    st["boost"] = conf
    cp_status[cp_id] = st
    return conf

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
        # als Fallback (manche Boxen übergeben sie im Subprotocol-Header)
        proto_hdr = websocket.headers.get("Sec-WebSocket-Protocol") or ""
        cp_id = proto_hdr.replace("ocpp1.6", "").strip() or "unknown"

    # Whitelist prüfen (falls gesetzt)
    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        log.info("connection rejected (403 Forbidden) for cp_id=%s", cp_id)
        # WS-Close 1008 (Policy Violation)
        await websocket.close(code=1008)
        return

    log.info("OCPP connect (ASGI): %s", cp_id)
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
        # cp_registry.pop(cp_id, None)  # optional aufräumen

@app.websocket("/ocpp")
async def ocpp_ws_root(websocket: WebSocket):
    await _serve_ocpp(websocket)

@app.websocket("/ocpp/{cp_id}")
async def ocpp_ws_with_id(websocket: WebSocket, cp_id: str):
    # Die CP-ID steckt in der URL – dennoch verwendet extract_cp_id_from_path
    await _serve_ocpp(websocket)

@app.websocket("/ocpp/{cp_id}/{tail:path}")
async def ocpp_ws_with_tail(websocket: WebSocket, cp_id: str, tail: str):
    # Fängt Sonderfälle wie /ocpp/<id>/<id> ab (manche Boxen hängen sie doppelt an)
    await _serve_ocpp(websocket)
