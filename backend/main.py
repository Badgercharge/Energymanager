# backend/main.py
import os
import json
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# OCPP 1.6 (ocpp==0.17.0)
from ocpp.v16 import call, call_result
from ocpp.v16 import ChargePoint as V16ChargePoint
from ocpp.routing import on
from ocpp.v16.enums import RegistrationStatus

# -----------------------------------------------------------------------------
# Einstellungen / ENV
# -----------------------------------------------------------------------------
APP_TITLE = "Home Charger EMS"
VERSION = "1.0.0"

# Whitelist optional: nur diese CP-ID(s) zulassen; leer = alle
CP_ID_ENV = os.getenv("CP_ID", "").strip()
KNOWN_CP_IDS = set([CP_ID_ENV]) if CP_ID_ENV else set()

# Netzparameter für Limit-Berechnung
PHASES = int(os.getenv("PHASES", "3") or "3")
VOLTAGE = float(os.getenv("VOLTAGE", "230") or "230")

# Preisquelle (aWATTar Deutschland)
PRICE_API_URL = os.getenv("PRICE_API_URL", "https://api.awattar.de/v1/marketdata")

# Wetter (Open-Meteo) – Standard: 94368 Radldorf
LAT = float(os.getenv("LAT", "48.83"))
LON = float(os.getenv("LON", "12.86"))

# CORS
ALLOW_ORIGINS = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")]

# -----------------------------------------------------------------------------
# Logging + Live-Buffer
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s:%(name)s:%(message)s",
)
log = logging.getLogger("backend")

LOG_BUFFER = deque(maxlen=500)


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
            )
        except Exception:
            pass


_buf = BufferHandler()
logging.getLogger().addHandler(_buf)
logging.getLogger("ocpp").addHandler(_buf)

# -----------------------------------------------------------------------------
# Hilfsfunktionen
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round01(x: float) -> float:
    return round(x * 10.0) / 10.0


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# -----------------------------------------------------------------------------
# Status / Registry
# -----------------------------------------------------------------------------
cp_status: Dict[str, Dict[str, Any]] = {}
cp_registry: Dict[str, "CentralSystem"] = {}

# In-Memory Eco-Konfig
ECO_CONFIG: Dict[str, Any] = {"sunny_kw": 11.0, "cloudy_kw": 3.7}

# Caches
_price_cache: Dict[str, Any] = {"ts": None, "data": None}
_weather_cache: Dict[str, Any] = {"ts": None, "data": None}


def normalize_status(s: Optional[str]) -> str:
    if not s:
        return "unknown"
    s = s.strip()
    return s.replace(" ", "_").replace("-", "_").lower()


# -----------------------------------------------------------------------------
# OCPP Central System
# -----------------------------------------------------------------------------
class CentralSystem(V16ChargePoint):
    def __init__(self, id: str, connection):
        super().__init__(id, connection)

    async def push_limit_kw(self, kw: float, connector_id: int = 1, phases: Optional[int] = None, voltage: Optional[float] = None):
        """Pusht ein TxProfile-Limit (Ampere) basierend auf kW."""
        ph = phases or PHASES
        volt = voltage or VOLTAGE
        # typisches Mindestlimit: 6 A, viele EVSE akzeptieren 0,1 A Schritte
        amps = max(6.0, _round01((kw * 1000.0) / (volt * ph)))
        profile = {
            "chargingProfileId": 2001,
            "stackLevel": 2,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "chargingRateUnit": "A",
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": amps, "numberPhases": ph}
                ],
            },
        }
        try:
            res = await self.call(call.SetChargingProfile(connector_id=connector_id, cs_charging_profiles=profile))
            st = cp_status.get(self.id) or {"id": self.id}
            st["target_kw"] = round(kw, 3)
            st["last_profile_status"] = getattr(res, "status", "")
            st["last_seen"] = now_iso()
            cp_status[self.id] = st
            logging.getLogger("ocpp").info(
                "%s: SetChargingProfile sent (%.1f A ~ %.2f kW) -> %s",
                self.id,
                amps,
                kw,
                getattr(res, "status", ""),
            )
        except Exception as e:
            logging.getLogger("ocpp").warning("%s: push profile failed: %s", self.id, e)

    async def clear_profile(self, connector_id: int = 1):
        try:
            await self.call(call.ClearChargingProfile(connector_id=connector_id))
            logging.getLogger("ocpp").info("%s: ClearChargingProfile ok", self.id)
        except Exception as e:
            logging.getLogger("ocpp").warning("%s: ClearChargingProfile failed: %s", self.id, e)

    # -------------------- OCPP Handlers --------------------
    @on("BootNotification")
    async def on_boot(self, charge_point_vendor: str, charge_point_model: str, **kwargs):
        st = cp_status.get(self.id) or {"id": self.id}
        st["vendor"] = charge_point_vendor
        st["model"] = charge_point_model
        st["status"] = "available"
        st["last_seen"] = now_iso()
        cp_status[self.id] = st
        logging.getLogger("ocpp").info("%s: BootNotification model=%s vendor=%s", self.id, charge_point_model, charge_point_vendor)
        return call_result.BootNotificationPayload(current_time=now_iso(), interval=30, status=RegistrationStatus.accepted)

    @on("Heartbeat")
    async def on_heartbeat(self):
        st = cp_status.get(self.id) or {"id": self.id}
        st["last_seen"] = now_iso()
        cp_status[self.id] = st
        return call_result.HeartbeatPayload(current_time=now_iso())

    @on("StatusNotification")
    async def on_status(self, connector_id: int, error_code: str, status: str, **kwargs):
        st = cp_status.get(self.id) or {"id": self.id}
        st["status"] = normalize_status(status)
        st["error_code"] = error_code
        st["last_seen"] = now_iso()
        cp_status[self.id] = st
        return call_result.StatusNotificationPayload()

    @on("StartTransaction")
    async def on_start_transaction(self, connector_id: int, id_tag: str, timestamp: str, meter_start: int, reservation_id: Optional[int] = None):
        st = cp_status.get(self.id) or {"id": self.id}
        st["tx_active"] = True
        st["session"] = {
            "start": timestamp or now_iso(),
            "end": None,
            "est_end": None,
            "start_meter_wh": float(meter_start),
            "last_meter_wh": float(meter_start),
        }
        st["energy_kwh_session"] = 0.0
        st["last_seen"] = now_iso()
        cp_status[self.id] = st
        logging.getLogger("ocpp").info("%s: StartTransaction meter_start=%.1f Wh", self.id, float(meter_start))
        return call_result.StartTransactionPayload(transaction_id=1, id_tag_info={"status": "Accepted"})

    @on("MeterValues")
    async def on_meter_values(self, connector_id: int, meter_value: list, transaction_id: Optional[int] = None):
        power_kw = None
        soc = None
        latest_energy_wh = None

        for mv in meter_value or []:
            for sv in (mv.get("sampledValue") or []):
                meas = (sv.get("measurand") or "").strip()
                unit = (sv.get("unit") or "").strip()
                val_str = sv.get("value")
                try:
                    val = float(val_str)
                except Exception:
                    continue
                lower = meas.lower()
                if "power.active.import" in lower:
                    power_kw = val if unit.lower() == "kw" else (val / 1000.0)
                elif "energy.active.import.register" in lower:
                    latest_energy_wh = val if unit.lower() == "wh" else (val * 1000.0)
                elif lower == "soc" or meas == "SoC":
                    try:
                        soc = int(val)
                    except Exception:
                        pass

        st = cp_status.get(self.id) or {"id": self.id}
        sess = st.get("session") or {}

        if latest_energy_wh is not None:
            if "start_meter_wh" not in sess or sess.get("start_meter_wh") is None:
                sess["start_meter_wh"] = latest_energy_wh
            sess["last_meter_wh"] = latest_energy_wh
            diff_wh = max(0.0, latest_energy_wh - float(sess.get("start_meter_wh") or 0.0))
            st["energy_kwh_session"] = round(diff_wh / 1000.0, 3)
            st["session"] = sess

        if power_kw is not None:
            st["power_kw"] = round(power_kw, 3)
        if soc is not None:
            st["soc"] = soc

        st["last_seen"] = now_iso()
        cp_status[self.id] = st
        return call_result.MeterValuesPayload()

    @on("StopTransaction")
    async def on_stop_transaction(self, transaction_id: int, id_tag: str, timestamp: str, meter_stop: int, transaction_data: Optional[list] = None, reason: Optional[str] = None):
        st = cp_status.get(self.id) or {"id": self.id}
        sess = st.get("session") or {}
        try:
            stop_wh = float(meter_stop)
            if "start_meter_wh" in sess and sess.get("start_meter_wh") is not None:
                diff_wh = max(0.0, stop_wh - float(sess["start_meter_wh"]))
                st["energy_kwh_session"] = round(diff_wh / 1000.0, 3)
            sess["last_meter_wh"] = stop_wh
        except Exception:
            pass
        sess["end"] = timestamp or now_iso()
        st["session"] = sess
        st["tx_active"] = False
        st["last_seen"] = now_iso()
        cp_status[self.id] = st
        logging.getLogger("ocpp").info("%s: StopTransaction energy_kwh_session=%.3f", self.id, st.get("energy_kwh_session", 0.0))
        return call_result.StopTransactionPayload()

    @on("DataTransfer")
    async def on_data_transfer(self, vendor_id: str, message_id: Optional[str] = None, data: Optional[str] = None):
        logging.getLogger("ocpp").debug("%s: DataTransfer vendor_id=%s message_id=%s", self.id, vendor_id, message_id)
        return call_result.DataTransferPayload(status="Accepted")


# -----------------------------------------------------------------------------
# WS-Adapter (Starlette WebSocket -> ocpp connection)
# -----------------------------------------------------------------------------
class WSConn:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._closed = False

    async def recv(self) -> str:
        return await self.ws.receive_text()

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


# -----------------------------------------------------------------------------
# FastAPI App + CORS
# -----------------------------------------------------------------------------
app = FastAPI(title=APP_TITLE, version=VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# Health / Root
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return {"ok": True, "app": APP_TITLE, "version": VERSION, "time": now_iso()}


# -----------------------------------------------------------------------------
# Live Logs
# -----------------------------------------------------------------------------
@app.get("/api/logs")
def api_logs(limit: int = 200):
    limit = max(10, min(500, int(limit or 200)))
    return list(LOG_BUFFER)[-limit:]


# -----------------------------------------------------------------------------
# OCPP WebSocket
# -----------------------------------------------------------------------------
@app.websocket("/ocpp/{cp_id}")
async def ocpp_ws(websocket: WebSocket, cp_id: str):
    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        await websocket.close(code=4030)
        log.info("Reject WS for unknown CP-ID %s", cp_id)
        return

    await websocket.accept(subprotocol="ocpp1.6")
    log.info("OCPP connect (ASGI): %s", cp_id)

    conn = WSConn(websocket)
    cp = CentralSystem(cp_id, conn)
    cp_registry[cp_id] = cp

    st = cp_status.get(cp_id) or {"id": cp_id}
    st.setdefault("status", "unknown")
    st["last_seen"] = now_iso()
    cp_status[cp_id] = st

    try:
        await cp.start()
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
        st["status"] = "disconnected"
        st["last_seen"] = now_iso()
        cp_status[cp_id] = st


# -----------------------------------------------------------------------------
# Preis (aWATTar) – /api/price
# -----------------------------------------------------------------------------
@app.get("/api/price")
async def api_price():
    try:
        # Cache 2 Minuten
        ts = _price_cache.get("ts")
        if ts and datetime.now(timezone.utc) - ts < timedelta(seconds=120):
            return _price_cache["data"]

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(PRICE_API_URL)
            r.raise_for_status()
            data = r.json()

        items = data.get("data") or []
        # marketprice ist EUR/MWh -> ct/kWh = price / 10
        series = []
        now_ts = datetime.now(timezone.utc).timestamp() * 1000.0
        for it in items:
            start = float(it.get("start_timestamp") or 0)
            end = float(it.get("end_timestamp") or 0)
            price_eur_per_mwh = float(it.get("marketprice") or 0.0)
            ct_per_kwh = price_eur_per_mwh / 10.0
            series.append(
                {"start": start, "end": end, "ct_per_kwh": round(ct_per_kwh, 3)}
            )

        current = None
        prices_24h = []
        now_ms = now_ts
        for s in series:
            prices_24h.append(s["ct_per_kwh"])
            if s["start"] <= now_ms <= s["end"]:
                current = s["ct_per_kwh"]
        median = None
        if prices_24h:
            sorted_vals = sorted(prices_24h)
            n = len(sorted_vals)
            median = (
                (sorted_vals[n // 2] if n % 2 == 1 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0)
            )

        payload = {
            "as_of": now_iso(),
            "current_ct_per_kwh": current,
            "median_ct_per_kwh": median,
            "below_or_equal_median": (current is not None and median is not None and current <= median),
            "series": series[:96],  # bis 24h x 15min (aWATTar kann stündlich sein; ok)
        }
        _price_cache["ts"] = datetime.now(timezone.utc)
        _price_cache["data"] = payload
        return payload
    except Exception as e:
        log.warning("price fetch failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)


# -----------------------------------------------------------------------------
# Wetter (Open-Meteo) – /api/weather
# -----------------------------------------------------------------------------
@app.get("/api/weather")
async def api_weather():
    try:
        ts = _weather_cache.get("ts")
        if ts and datetime.now(timezone.utc) - ts < timedelta(seconds=120):
            return _weather_cache["data"]

        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={LAT}&longitude={LON}"
            "&current=cloud_cover,shortwave_radiation,temperature_2m,weather_code&timezone=auto"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()

        current = (data.get("current") or {})
        payload = {
            "as_of": now_iso(),
            "cloud_cover": current.get("cloud_cover"),
            "shortwave_radiation": current.get("shortwave_radiation"),
            "temperature_2m": current.get("temperature_2m"),
            "weather_code": current.get("weather_code"),
        }
        _weather_cache["ts"] = datetime.now(timezone.utc)
        _weather_cache["data"] = payload
        return payload
    except Exception as e:
        log.warning("weather fetch failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)


# -----------------------------------------------------------------------------
# Eco-Konfig – /api/config/eco
# -----------------------------------------------------------------------------
@app.get("/api/config/eco")
def get_eco_config():
    return ECO_CONFIG


@app.post("/api/config/eco")
async def set_eco_config(body: Dict[str, Any]):
    sunny = body.get("sunny_kw")
    cloudy = body.get("cloudy_kw")
    if isinstance(sunny, (int, float)):
        ECO_CONFIG["sunny_kw"] = clamp(float(sunny), 0.0, 22.0)
    if isinstance(cloudy, (int, float)):
        ECO_CONFIG["cloudy_kw"] = clamp(float(cloudy), 0.0, 22.0)
    return {"ok": True, **ECO_CONFIG}


# -----------------------------------------------------------------------------
# Points / Status
# -----------------------------------------------------------------------------
@app.get("/api/points")
def api_points():
    # Liste aller Ladepunkte
    return list(cp_status.values())


@app.get("/api/points/{cp_id}")
def api_point(cp_id: str):
    st = cp_status.get(cp_id)
    if not st:
        return JSONResponse({"error": "not found"}, status_code=404)
    return st


@app.get("/api/stats")
def api_stats():
    total_points = len(cp_status)
    active = sum(1 for s in cp_status.values() if s.get("status") not in (None, "disconnected", "unknown"))
    return {"points_total": total_points, "points_active": active, "time": now_iso()}


# -----------------------------------------------------------------------------
# Boost & Manuelles Limit
# -----------------------------------------------------------------------------
@app.get("/api/points/{cp_id}/boost")
async def get_boost(cp_id: str, kw: Optional[float] = None):
    """Kompatibel zu deinem Frontend (GET). Setzt Limit auf 11 kW (oder ?kw=...)."""
    return await _apply_limit(cp_id, kw if kw is not None else 11.0)


@app.post("/api/points/{cp_id}/boost")
async def post_boost(cp_id: str, body: Dict[str, Any]):
    kw = body.get("kw", 11.0)
    return await _apply_limit(cp_id, float(kw))


@app.post("/api/points/{cp_id}/set_kw")
async def set_kw(cp_id: str, body: Dict[str, Any]):
    """Manuelles kW-Setzen vom Frontend."""
    if "kw" not in body:
        return JSONResponse({"error": "kw missing"}, status_code=400)
    kw = clamp(float(body["kw"]), 0.0, 22.0)
    return await _apply_limit(cp_id, kw)


async def _apply_limit(cp_id: str, kw: float):
    cp = cp_registry.get(cp_id)
    if not cp:
        return JSONResponse({"error": "charger not connected"}, status_code=409)
    await cp.push_limit_kw(kw)
    st = cp_status.get(cp_id) or {"id": cp_id}
    st["target_kw"] = round(kw, 3)
    st["last_seen"] = now_iso()
    cp_status[cp_id] = st
    return {"ok": True, "id": cp_id, "target_kw": st["target_kw"], "last_profile_status": st.get("last_profile_status")}


# -----------------------------------------------------------------------------
# Debug-Routen
# -----------------------------------------------------------------------------
@app.get("/debug/routes")
def debug_routes():
    routes = []
    for r in app.router.routes:
        try:
            path = getattr(r, "path", "")
            methods = sorted(getattr(r, "methods", []))
            routes.append({"path": path, "methods": methods})
        except Exception:
            pass
    routes.sort(key=lambda x: x["path"])
    return routes
