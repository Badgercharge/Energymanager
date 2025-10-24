# backend/ocpp_cs.py
import os
import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from ocpp.charge_point import ChargePoint
from ocpp.routing import on
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    RegistrationStatus,
    AuthorizationStatus,
    ChargePointStatus,
)

log = logging.getLogger("ocpp")

# --------------------------------------------------------------------
# Whitelist / Defaults
# --------------------------------------------------------------------
def _parse_ids(val: str) -> set[str]:
    if not val:
        return set()
    return {x.strip() for x in val.split(",") if x.strip()}

DEFAULT_CP_ID = os.getenv("DEFAULT_CP_ID", "")
KNOWN_CP_IDS: set[str] = _parse_ids(os.getenv("KNOWN_CP_IDS", ""))

# --------------------------------------------------------------------
# Globale Registry/Status (wird von main.py exportiert)
# --------------------------------------------------------------------
cp_registry: Dict[str, "CentralSystem"] = {}
cp_status: Dict[str, Dict[str, Any]] = {}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def extract_cp_id_from_path(path: str) -> str:
    """
    Versucht, aus einem WS-Pfad /ocpp[/<id>[/...]] eine CP-ID zu ermitteln.
    Fallback: DEFAULT_CP_ID.
    """
    parts = [p for p in path.split("/") if p]
    # Nimm das letzte Segment, wenn es nicht 'ocpp' ist
    if parts and parts[-1].lower() != "ocpp":
        return parts[-1]
    if len(parts) >= 2 and parts[0].lower() == "ocpp":
        return parts[1]
    return DEFAULT_CP_ID or "unknown"

def round_to_01_amp(x: float) -> float:
    # OCPP-Schema verlangt multipleOf 0.1
    return float(Decimal(x).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

def kw_from_amps(amps: float, phases: int = 3, voltage: float = 230.0) -> float:
    return max(0.0, amps * voltage * max(1, phases) / 1000.0)

# --------------------------------------------------------------------
# Central System (Server-seitige ChargePoint-Instanz)
# --------------------------------------------------------------------
class CentralSystem(ChargePoint):
    def __init__(self, cp_id: str, connection):
        super().__init__(cp_id, connection)
        self.cp_id = cp_id
        self.transaction_id: Optional[int] = None
        self.meter_start_Wh: Optional[int] = None

        # Laufende Telemetriedaten (werden in cp_status gespiegelt)
        self.power_W: float = 0.0
        self.energy_session_Wh: float = 0.0
        self.soc_percent: Optional[int] = None
        self.status: str = "Unknown"

        # Initialstatus bereitstellen
        cp_status[self.cp_id] = {
            "id": self.cp_id,
            "status": self.status,
            "last_seen": now_iso(),
            "power_kw": 0.0,
            "energy_kwh_session": 0.0,
            "tx_active": False,
            "soc": None,
            "target_kw": None,   # kann vom Scheduler gesetzt werden
            "mode": None,        # kann vom Scheduler/Frontend gesetzt werden
            "session": None,     # {"start": iso, "energy_kwh": float, "est_end": iso?}
        }

    # -------------------- Client -> Server Handlers --------------------

    @on(Action.boot_notification)
    async def on_boot(self, charge_point_model: str = None, charge_point_vendor: str = None, **kwargs):
        log.info("%s: BootNotification model=%s vendor=%s", self.cp_id, charge_point_model, charge_point_vendor)
        self.status = "Available"
        st = cp_status.get(self.cp_id, {})
        st.update({
            "status": self.status,
            "last_seen": now_iso(),
            "model": charge_point_model,
            "vendor": charge_point_vendor,
        })
        cp_status[self.cp_id] = st
        # ocpp 2.x: call_result.BootNotification (ohne Payload-Suffix)
        return call_result.BootNotification(
            current_time=now_iso(),
            interval=30,
            status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kwargs):
        cp_status[self.cp_id]["last_seen"] = now_iso()
        return call_result.Heartbeat(current_time=now_iso())

    @on(Action.status_notification)
    async def on_status_notification(self, connector_id: int, status: ChargePointStatus, **kwargs):
        # status ist bereits ein Enum
        self.status = status.value if hasattr(status, "value") else str(status)
        st = cp_status[self.cp_id]
        st["status"] = self.status
        st["last_seen"] = now_iso()
        # tx_active grob aus Status ableiten
        st["tx_active"] = self.status.lower() in ("charging", "preparing")
        return call_result.StatusNotification()

    @on(Action.authorize)
    async def on_authorize(self, id_tag: str, **kwargs):
        # Immer akzeptieren (Demo)
        return call_result.Authorize(id_tag_info={"status": AuthorizationStatus.accepted})

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id: int, id_tag: str, meter_start: int, timestamp: str, **kwargs):
        # Transaction-ID von echten Boxen kommt im Response normalerweise vom CS; wir verwenden 1 als Demo.
        self.transaction_id = 1
        self.meter_start_Wh = meter_start
        self.energy_session_Wh = 0.0
        st = cp_status[self.cp_id]
        st["tx_active"] = True
        st["session"] = {"start": timestamp, "energy_kwh": 0.0, "est_end": None}
        log.info("%s: StartTransaction connector=%s meter_start=%s", self.cp_id, connector_id, meter_start)
        return call_result.StartTransaction(
            transaction_id=self.transaction_id,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on(Action.stop_transaction)
    async def on_stop_transaction(self, meter_stop: int, timestamp: str, **kwargs):
        # Session finalisieren
        if self.meter_start_Wh is not None:
            self.energy_session_Wh = max(0.0, float(meter_stop - self.meter_start_Wh))
        st = cp_status[self.cp_id]
        st["tx_active"] = False
        sess = st.get("session") or {}
        sess["end"] = timestamp
        sess["energy_kwh"] = round((self.energy_session_Wh or 0.0) / 1000.0, 3)
        st["session"] = sess
        log.info("%s: StopTransaction meter_stop=%s, energy_kwh=%.3f", self.cp_id, meter_stop, sess["energy_kwh"])
        return call_result.StopTransaction()

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id: int, meter_value: list, **kwargs):
        """
        Erwartete sampledValue measurands:
        - Energy.Active.Import.Register (Wh, kumulativ)
        - Power.Active.Import (W, Momentanleistung)
        - SoC (Percent)
        """
        try:
            for entry in meter_value or []:
                sv_list = entry.get("sampledValue", [])
                for sv in sv_list:
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

        # Status aktualisieren
        st = cp_status[self.cp_id]
        st["last_seen"] = now_iso()
        st["power_kw"] = round(max(0.0, self.power_W) / 1000.0, 3)
        st["energy_kwh_session"] = round((self.energy_session_Wh or 0.0) / 1000.0, 3)
        if self.soc_percent is not None:
            st["soc"] = self.soc_percent

        return call_result.MeterValues()

    # -------------------- Server -> Client (aus Scheduler aufrufbar) ----

    async def set_limit_amps(self, limit_amps: float, duration_s: int = 3600) -> bool:
        """
        Schiebt ein SetChargingProfile mit Ampere-Limit (0,1 A-Auflösung).
        """
        lim = round_to_01_amp(limit_amps)
        profile = {
            "chargingProfileId": 1,
            "stackLevel": 0,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "chargingRateUnit": "A",  # explizit String, vermeidet Enum-Mismatches
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": lim}],
                "duration": duration_s,
            },
        }
        try:
            res = await self.call(
                call.SetChargingProfile(connector_id=1, cs_charging_profiles=profile)
            )
            ok = getattr(res, "status", "Rejected")
            if str(ok).lower() == "accepted":
                # Ziel-Leistung in Status spiegeln (nur Info)
                target_kw = round(kw_from_amps(lim), 2)
                st = cp_status.get(self.cp_id, {})
                st["target_kw"] = target_kw
                cp_status[self.cp_id] = st
                log.info("%s: SetChargingProfile accepted (%.1f A ~ %.2f kW)", self.cp_id, lim, target_kw)
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
            # Einige Boxen verlangen transaction_id, andere nicht – hier ohne, damit generischer
            res = await self.call(call.RemoteStopTransaction(transaction_id=self.transaction_id or 1))
            return str(getattr(res, "status", "")).lower() == "accepted"
        except Exception as e:
            log.warning("%s: remote_stop failed: %s", self.cp_id, e)
            return False
