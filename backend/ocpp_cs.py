# backend/ocpp_cs.py
import os
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from ocpp.v16 import call, call_result
from ocpp.v16 import ChargePoint as CP
from ocpp.v16.enums import Action, RegistrationStatus, AuthorizationStatus, ChargePointStatus
from ocpp.routing import on

log = logging.getLogger("ocpp")

def _parse_ids(val: str) -> set[str]:
    if not val:
        return set()
    return {x.strip() for x in val.split(",") if x.strip()}

DEFAULT_CP_ID = os.getenv("DEFAULT_CP_ID", "")
KNOWN_CP_IDS: set[str] = _parse_ids(os.getenv("KNOWN_CP_IDS", ""))

cp_registry: Dict[str, "CentralSystem"] = {}
cp_status: Dict[str, Dict[str, Any]] = {}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def extract_cp_id_from_path(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1].lower() != "ocpp":
        return parts[-1]
    if len(parts) >= 2 and parts[0].lower() == "ocpp":
        return parts[1]
    return DEFAULT_CP_ID or "unknown"

def round_to_01_amp(x: float) -> float:
    return float(Decimal(x).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

def kw_from_amps(amps: float, phases: int = 3, voltage: float = 230.0) -> float:
    return max(0.0, amps * voltage * max(1, phases) / 1000.0)

class CentralSystem(CP):
    def __init__(self, cp_id: str, connection):
        super().__init__(cp_id, connection)  # wichtig: von ocpp.v16.CP erben
        self.cp_id = cp_id
        self.transaction_id: Optional[int] = None
        self.meter_start_Wh: Optional[int] = None
        self.power_W: float = 0.0
        self.energy_session_Wh: float = 0.0
        self.soc_percent: Optional[int] = None
        self.status: str = "Unknown"

        cp_status[self.cp_id] = {
            "id": self.cp_id,
            "status": self.status,
            "last_seen": now_iso(),
            "power_kw": 0.0,
            "energy_kwh_session": 0.0,
            "tx_active": False,
            "soc": None,
            "target_kw": None,
            "mode": None,
            "session": None,
        }

    # ============ CP -> CS ============

    @on(Action.boot_notification)
    async def on_boot(self, charge_point_model: str = None, charge_point_vendor: str = None, **kwargs):
        log.info("%s: BootNotification model=%s vendor=%s", self.cp_id, charge_point_model, charge_point_vendor)
        self.status = "Available"
        st = cp_status[self.cp_id]
        st.update({"status": self.status, "last_seen": now_iso(), "model": charge_point_model, "vendor": charge_point_vendor})
        return call_result.BootNotification(current_time=now_iso(), interval=30, status=RegistrationStatus.accepted)

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kwargs):
        cp_status[self.cp_id]["last_seen"] = now_iso()
        return call_result.Heartbeat(current_time=now_iso())

    @on(Action.status_notification)
    async def on_status_notification(self, connector_id: int, status: ChargePointStatus, **kwargs):
        self.status = status.value if hasattr(status, "value") else str(status)
        st = cp_status[self.cp_id]
        st["status"] = self.status
        st["last_seen"] = now_iso()
        st["tx_active"] = self.status.lower() in ("charging", "preparing")
        return call_result.StatusNotification()

    @on(Action.authorize)
    async def on_authorize(self, id_tag: str, **kwargs):
        return call_result.Authorize(id_tag_info={"status": AuthorizationStatus.accepted})

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id: int, id_tag: str, meter_start: int, timestamp: str, **kwargs):
        self.transaction_id = 1
        self.meter_start_Wh = meter_start
        self.energy_session_Wh = 0.0
        st = cp_status[self.cp_id]
        st["tx_active"] = True
        st["session"] = {"start": timestamp, "energy_kwh": 0.0, "est_end": None}
        return call_result.StartTransaction(transaction_id=self.transaction_id, id_tag_info={"status": AuthorizationStatus.accepted})

    @on(Action.stop_transaction)
    async def on_stop_transaction(self, meter_stop: int, timestamp: str, **kwargs):
        if self.meter_start_Wh is not None:
            self.energy_session_Wh = max(0.0, float(meter_stop - self.meter_start_Wh))
        st = cp_status[self.cp_id]
        st["tx_active"] = False
        sess = st.get("session") or {}
        sess["end"] = timestamp
        sess["energy_kwh"] = round((self.energy_session_Wh or 0.0) / 1000.0, 3)
        st["session"] = sess
        return call_result.StopTransaction()

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id: int, meter_value: list, **kwargs):
        try:
            for entry in meter_value or []:
                for sv in entry.get("sampledValue", []):
                    meas = (sv.get("measurand") or "").strip()
                    val = sv.get("value")
                    if val is None:
                        continue
                    if meas == "Power.Active.Import":
                        self.power_W = float(val)
                    elif meas == "Energy.Active.Import.Register":
                        wh = float(val)
                        if self.meter_start_Wh is None:
                            self.meter_start_Wh = int(wh)
                        self.energy_session_Wh = max(0.0, wh - self.meter_start_Wh)
                    elif meas == "SoC":
                        self.soc_percent = int(float(val))
        except Exception as e:
            log.warning("%s: meter_values parse error: %s", self.cp_id, e)

        st = cp_status[self.cp_id]
        st["last_seen"] = now_iso()
        st["power_kw"] = round(max(0.0, self.power_W) / 1000.0, 3)
        st["energy_kwh_session"] = round((self.energy_session_Wh or 0.0) / 1000.0, 3)
        if self.soc_percent is not None:
            st["soc"] = self.soc_percent
        return call_result.MeterValues()

    # ============ CS -> CP ============

    async def set_limit_amps(self, limit_amps: float, duration_s: int = 3600) -> bool:
        lim = round_to_01_amp(limit_amps)
        profile = {
            "chargingProfileId": 1,
            "stackLevel": 0,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "chargingRateUnit": "A",
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": lim}],
                "duration": duration_s,
            },
        }
        try:
            res = await self.call(call.SetChargingProfile(connector_id=1, cs_charging_profiles=profile))
            ok = getattr(res, "status", "Rejected")
            if str(ok).lower() == "accepted":
                cp_status[self.cp_id]["target_kw"] = round(kw_from_amps(lim), 2)
                log.info("%s: SetChargingProfile accepted (%.1f A)", self.cp_id, lim)
                return True
            log.warning("%s: SetChargingProfile not accepted: %s", self.cp_id, ok)
            return False
        except Exception as e:
            log.warning("%s: push profile failed: %s", self.cp_id, e)
            return False

    async def remote_start(self, id_tag: str = "REMOTE") -> bool:
        try:
            res = await self.call(call.RemoteStartTransaction(id_tag=id_tag, connector_id=1))
            return str(getattr(res, "status", "")).lower() == "accepted"
        except Exception as e:
            log.warning("%s: remote_start failed: %s", self.cp_id, e)
            return False

    async def remote_stop(self) -> bool:
        try:
            res = await self.call(call.RemoteStopTransaction(transaction_id=self.transaction_id or 1))
            return str(getattr(res, "status", "")).lower() == "accepted"
        except Exception as e:
            log.warning("%s: remote_stop failed: %s", self.cp_id, e)
            return False
