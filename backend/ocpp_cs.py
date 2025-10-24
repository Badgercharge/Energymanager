# backend/ocpp_cs.py
import os
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone

from ocpp.v16 import call, call_result
from ocpp.v16 import ChargePoint as CP
from ocpp.v16.enums import Action, RegistrationStatus
from ocpp.routing import on

log = logging.getLogger("ocpp")

# Feste CP-ID mit ENV-Override
DEFAULT_CP_ID = os.getenv("DEFAULT_CP_ID", "504000093")
KNOWN_CP_IDS = {s.strip() for s in os.getenv("KNOWN_CP_IDS", DEFAULT_CP_ID).split(",") if s.strip()}

# In-Memory Registry / Status
cp_registry = {}  # cp_id -> CentralSystem
cp_status = {}    # cp_id -> dict

def extract_cp_id_from_path(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "ocpp":
        return parts[1]
    if len(parts) == 1 and parts[0].lower() == "ocpp":
        return DEFAULT_CP_ID
    return DEFAULT_CP_ID

def q01_amp(value: float) -> float:
    # OCPP 1.6 verlangt multiples of 0.1 A für chargingSchedulePeriod.limit
    return float(Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

class CentralSystem(CP):
    """Server-seitige OCPP 1.6-Implementierung (CS)."""
    def __init__(self, cp_id: str, connection):
        super().__init__(cp_id, connection)
        self.cp_id = cp_id
        self._transaction_id = None
        self._energy_start_Wh = None

        cp_status[self.cp_id] = {
            "id": self.cp_id,
            "status": "Unknown",
            "power_w": 0,
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "session": None,
        }
        # In Registry eintragen (falls dein main.py das nicht übernimmt)
        cp_registry[self.cp_id] = self

    # ====== CP -> CS Handlers (snake_case Actions in ocpp 2.x) ======

    @on(Action.boot_notification)
    async def on_boot(self, charge_point_model: str, charge_point_vendor: str, **kwargs):
        log.info("BootNotification from %s: vendor=%s model=%s", self.cp_id, charge_point_vendor, charge_point_model)
        cp_status[self.cp_id]["last_seen"] = datetime.now(timezone.utc).isoformat()
        return call_result.BootNotificationPayload(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=30,
            status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kwargs):
        cp_status[self.cp_id]["last_seen"] = datetime.now(timezone.utc).isoformat()
        return call_result.HeartbeatPayload(current_time=datetime.now(timezone.utc).isoformat())

    @on(Action.status_notification)
    async def on_status(self, connector_id: int, status: str, **kwargs):
        cp_status[self.cp_id]["status"] = str(status)
        cp_status[self.cp_id]["last_seen"] = datetime.now(timezone.utc).isoformat()
        return call_result.StatusNotificationPayload()

    @on(Action.authorize)
    async def on_authorize(self, id_tag: str, **kwargs):
        cp_status[self.cp_id]["last_seen"] = datetime.now(timezone.utc).isoformat()
        return call_result.AuthorizePayload(id_tag_info={"status": "Accepted"})

    @on(Action.start_transaction)
    async def on_start_tx(self, connector_id: int, id_tag: str, meter_start: int, timestamp: str, **kwargs):
        self._transaction_id = int(datetime.now(timezone.utc).timestamp())
        self._energy_start_Wh = int(meter_start) if meter_start is not None else 0
        cp_status[self.cp_id]["status"] = "Charging"
        cp_status[self.cp_id]["session"] = {
            "transaction_id": self._transaction_id,
            "start_time": timestamp,
            "start_Wh": self._energy_start_Wh,
            "kwh": 0.0,
            "eta": None,
        }
        cp_status[self.cp_id]["last_seen"] = datetime.now(timezone.utc).isoformat()
        return call_result.StartTransactionPayload(
            transaction_id=self._transaction_id,
            id_tag_info={"status": "Accepted"},
        )

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id: int, meter_value: list, **kwargs):
        now_iso = datetime.now(timezone.utc).isoformat()
        power_w = None
        energy_Wh = None

        for mv in meter_value or []:
            for sv in mv.get("sampledValue", []):
                meas = (sv.get("measurand") or "").strip()
                val = sv.get("value")
                try:
                    if meas == "Power.Active.Import":
                        power_w = int(float(val))
                    elif meas == "Energy.Active.Import.Register":
                        energy_Wh = int(float(val))
                except Exception:
                    pass

        if power_w is not None:
            cp_status[self.cp_id]["power_w"] = power_w
        cp_status[self.cp_id]["last_seen"] = now_iso

        sess = cp_status[self.cp_id].get("session")
        if sess and energy_Wh is not None and self._energy_start_Wh is not None:
            delta_Wh = max(0, energy_Wh - self._energy_start_Wh)
            sess["kwh"] = round(delta_Wh / 1000.0, 3)

        return call_result.MeterValuesPayload()

    @on(Action.stop_transaction)
    async def on_stop_tx(self, meter_stop: int = None, timestamp: str = None, **kwargs):
        sess = cp_status[self.cp_id].get("session")
        if sess and (meter_stop is not None) and (self._energy_start_Wh is not None):
            delta_Wh = max(0, int(meter_stop) - int(self._energy_start_Wh))
            sess["kwh"] = round(delta_Wh / 1000.0, 3)
        cp_status[self.cp_id]["status"] = "Available"
        cp_status[self.cp_id]["last_seen"] = datetime.now(timezone.utc).isoformat()
        self._transaction_id = None
        self._energy_start_Wh = None
        return call_result.StopTransactionPayload(id_tag_info={"status": "Accepted"})

    # ====== CS -> CP Helper ======
    async def set_current_limit_amps(self, limit_amps: float, duration_s: int = 3600) -> bool:
        try:
            limit_amps = q01_amp(limit_amps)
            payload = {
                "connectorId": 1,
                "csChargingProfiles": {
                    "chargingProfileId": 1,
                    "stackLevel": 0,
                    "chargingProfilePurpose": "TxProfile",
                    "chargingProfileKind": "Absolute",
                    "chargingSchedule": {
                        "chargingRateUnit": "A",
                        "chargingSchedulePeriod": [
                            {"startPeriod": 0, "limit": limit_amps}
                        ],
                        "duration": duration_s,
                    },
                },
            }
            res = await self.call(call.SetChargingProfile(**payload))
            log.info("SetChargingProfile %s: %.1f A -> %s", self.cp_id, limit_amps, getattr(res, "status", ""))
            return True
        except Exception as e:
            log.warning("push profile %s failed: %s", self.cp_id, e)
            return False

# Kompatibilitäts-Handler für Standalone websockets (falls genutzt)
async def on_connect(websocket, path, app=None):
    cp_id = extract_cp_id_from_path(path)
    if KNOWN_CP_IDS and cp_id not in KNOWN_CP_IDS:
        try:
            await websocket.close(code=4000, reason="Unknown CP-ID")
        except Exception:
            pass
        return
    cp = CentralSystem(cp_id, websocket)
    try:
        await cp.start()
    finally:
        cp_registry.pop(cp_id, None)

def get_cp(cp_id: str) -> CentralSystem | None:
    return cp_registry.get(cp_id)

async def push_current_limit(cp_id: str, limit_amps: float, duration_s: int = 3600) -> bool:
    cp = get_cp(cp_id)
    if not cp:
        log.warning("push_current_limit: CP %s not connected", cp_id)
        return False
    return await cp.set_current_limit_amps(limit_amps, duration_s)
