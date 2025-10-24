# backend/ocpp_cs.py
import os
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from ocpp.v16 import call, call_result
from ocpp.v16 import ChargePoint as V16ChargePoint
from ocpp.v16.enums import RegistrationStatus
from ocpp.routing import on

log = logging.getLogger("ocpp")

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _round01(x: float) -> float:
    # auf 0,1 runden (viele EVSE verlangen Limit in 0,1A)
    return round(x * 10.0) / 10.0

# Live-Status aller bekannten Ladepunkte (wird von main.py importiert)
cp_status: Dict[str, Dict[str, Any]] = {}
# Laufende ChargePoint-Instanzen (für SetChargingProfile etc.)
cp_registry: Dict[str, "CentralSystem"] = {}

# Optionale Whitelist (leer = alle zulassen)
KNOWN_CP_IDS = set([os.getenv("CP_ID", "504000093")]) if os.getenv("CP_ID") else set()

def normalize_status(s: Optional[str]) -> str:
    if not s:
        return "unknown"
    s = s.strip()
    return s.replace(" ", "_").replace("-", "_").lower()

class CentralSystem(V16ChargePoint):
    """
    OCPP 1.6 Central System (Server-Seite), kompatibel mit ocpp==0.17.0.
    """
    def __init__(self, id: str, connection):
        super().__init__(id, connection)

    # ----------------------
    # Helpers / Profile Push
    # ----------------------
    async def push_limit_kw(self, kw: float, connector_id: int = 1, phases: Optional[int] = None, voltage: Optional[float] = None):
        """Pusht ein TxProfile-Limit in Ampere basierend auf kW-Vorgabe."""
        try:
            phases = phases or int(os.getenv("PHASES", "3"))
        except Exception:
            phases = 3
        try:
            voltage = voltage or float(os.getenv("VOLTAGE", "230"))
        except Exception:
            voltage = 230.0

        # Mindeststrom ≥ 6 A (typisch)
        limit_amps = max(6.0, _round01((kw * 1000.0) / (voltage * phases)))

        profile = {
            "chargingProfileId": 2001,
            "stackLevel": 2,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "chargingRateUnit": "A",
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": limit_amps, "numberPhases": phases}
                ],
            },
        }

        req = call.SetChargingProfile(connector_id=connector_id, cs_charging_profiles=profile)
        try:
            res = await self.call(req)
            st = cp_status.get(self.id) or {"id": self.id}
            st["target_kw"] = kw
            st["last_seen"] = _iso_now()
            cp_status[self.id] = st
            log.info("%s: SetChargingProfile sent (%.1f A ~ %.2f kW) -> %s", self.id, limit_amps, kw, getattr(res, "status", ""))
        except Exception as e:
            log.warning("push profile %s failed: %s", self.id, e)

    async def clear_profile(self, connector_id: int = 1):
        try:
            await self.call(call.ClearChargingProfile(connector_id=connector_id))
            log.info("%s: ClearChargingProfile ok", self.id)
        except Exception as e:
            log.warning("%s: ClearChargingProfile failed: %s", self.id, e)

    # ----------------------
    # OCPP Request Handler
    # ----------------------
    @on("BootNotification")
    async def on_boot(self, charge_point_vendor: str, charge_point_model: str, **kwargs):
        st = cp_status.get(self.id) or {"id": self.id}
        st["vendor"] = charge_point_vendor
        st["model"] = charge_point_model
        st["status"] = "available"  # Initialstatus; echte Stati kommen per StatusNotification
        st["last_seen"] = _iso_now()
        cp_status[self.id] = st

        log.info("%s: BootNotification model=%s vendor=%s", self.id, charge_point_model, charge_point_vendor)
        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=30,
            status=RegistrationStatus.accepted,
        )

    @on("Heartbeat")
    async def on_heartbeat(self):
        st = cp_status.get(self.id) or {"id": self.id}
        st["last_seen"] = _iso_now()
        cp_status[self.id] = st
        return call_result.Heartbeat(current_time=_iso_now())

    @on("StatusNotification")
    async def on_status(self, connector_id: int, error_code: str, status: str, **kwargs):
        st = cp_status.get(self.id) or {"id": self.id}
        st["status"] = normalize_status(status)
        st["error_code"] = error_code
        st["last_seen"] = _iso_now()
        cp_status[self.id] = st
        return call_result.StatusNotification()

    @on("StartTransaction")
    async def on_start_transaction(self, connector_id: int, id_tag: str, timestamp: str, meter_start: int, reservation_id: Optional[int] = None):
        st = cp_status.get(self.id) or {"id": self.id}
        st["tx_active"] = True
        st["session"] = {
            "start": timestamp or _iso_now(),
            "end": None,
            "est_end": None,
            "start_meter_wh": float(meter_start),
            "last_meter_wh": float(meter_start),
        }
        st["energy_kwh_session"] = 0.0
        st["last_seen"] = _iso_now()
        cp_status[self.id] = st
        log.info("%s: StartTransaction meter_start=%.1f Wh", self.id, float(meter_start))
        # Einfache Zählung (oder echte ID-Strategie implementieren)
        return call_result.StartTransaction(transaction_id=1, id_tag_info={"status": "Accepted"})

    @on("MeterValues")
    async def on_meter_values(self, connector_id: int, meter_value: list, transaction_id: Optional[int] = None):
        power_kw = None
        soc = None
        latest_energy_wh = None

        for mv in meter_value or []:
            for sv in mv.get("sampledValue", []) or []:
                meas = (sv.get("measurand") or "").strip()
                unit = (sv.get("unit") or "").strip()
                val_str = sv.get("value")
                try:
                    val = float(val_str)
                except Exception:
                    continue

                m = meas.lower()
                if "power.active.import" in m:
                    # Leistung in kW
                    power_kw = val if unit.lower() == "kw" else val / 1000.0
                elif "energy.active.import.register" in m:
                    # Zählerstand absolut
                    latest_energy_wh = val if unit.lower() == "wh" else val * 1000.0
                elif m == "soc" or meas == "SoC":
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

        st["last_seen"] = _iso_now()
        cp_status[self.id] = st

        return call_result.MeterValues()

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

        sess["end"] = timestamp or _iso_now()
        st["session"] = sess
        st["tx_active"] = False
        st["last_seen"] = _iso_now()
        cp_status[self.id] = st
        log.info("%s: StopTransaction energy_kwh_session=%.3f", self.id, st.get("energy_kwh_session", 0.0))
        return call_result.StopTransaction()

    @on("DataTransfer")
    async def on_data_transfer(self, vendor_id: str, message_id: Optional[str] = None, data: Optional[str] = None):
        # KEBA sendet gelegentlich DataTransfer; wir bestätigen neutral
        log.debug("%s: DataTransfer vendor_id=%s message_id=%s", self.id, vendor_id, message_id)
        return call_result.DataTransfer(status="Accepted")
