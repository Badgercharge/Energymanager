# backend/ocpp_cs.py
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from ocpp.routing import on
from ocpp.v16 import ChargePoint as CP, call_result, call
from ocpp.v16.enums import (
    RegistrationStatus, Action, ChargingRateUnitType,
    ChargingProfilePurposeType, ChargingProfileKindType,
    AuthorizationStatus, DataTransferStatus,
)
from ocpp.v16.datatypes import ChargingSchedule, ChargingSchedulePeriod, ChargingProfile

from models import STATE, ChargePointState, ENERGY_LOGS

logger = logging.getLogger(__name__)

MIN_KW = 3.7
MAX_KW = 11.0

def amps_from_kw(kw: float, phases: int, voltage: float) -> float:
    kw = max(MIN_KW, min(MAX_KW, float(kw)))
    return max(0.0, kw * 1000.0 / (max(1.0, voltage) * max(1, phases)))

def _try_float(x):
    try:
        return float(x)
    except Exception:
        return None

def _round_to_0p1(x: float) -> float:
    # Exakt auf 0,1 runden, um OCPP multipleOf 0.1 einzuhalten
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))

class CentralSystem(CP):
    @on(Action.boot_notification)
    async def on_boot(self, charge_point_model, charge_point_vendor, **kwargs):
        cp_id = self.id
        STATE.setdefault(cp_id, ChargePointState(id=cp_id, connected=True))
        logger.info("Boot from %s (%s/%s)", cp_id, charge_point_vendor, charge_point_model)

        res = call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=30,
            status=RegistrationStatus.accepted,
        )

        # Wunsch-Konfiguration nach Boot (nicht blockierend)
        async def _post_boot_cfg():
            try:
                await self.call(call.ChangeConfiguration(key="MeterValueSampleInterval", value="15"))
                await self.call(call.ChangeConfiguration(
                    key="MeterValuesSampledData",
                    value="Energy.Active.Import.Register,Power.Active.Import,SoC,Current.Import,Voltage"
                ))
                logger.info("Requested MeterValues config for %s", cp_id)
            except Exception as e:
                logger.warning("ChangeConfiguration failed for %s: %s", cp_id, e)

        asyncio.create_task(_post_boot_cfg())
        return res

    @on(Action.authorize)
    async def on_authorize(self, id_tag, **kwargs):
        # Demo: alle Tags akzeptieren
        return call_result.Authorize(id_tag_info={"status": AuthorizationStatus.accepted})

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kwargs):
        st = STATE.get(self.id)
        if st:
            st.connected = True
            st.last_heartbeat = datetime.now(timezone.utc)
        return call_result.Heartbeat(current_time=datetime.now(timezone.utc).isoformat())

    @on(Action.status_notification)
    async def on_status(self, status, error_code=None, **kwargs):
        st = STATE.get(self.id)
        if st:
            st.connected = True
            st.cp_status = status
            st.error_code = error_code
            if str(status).lower() == "charging":
                st.tx_active = True
        return call_result.StatusNotification()

    @on(Action.start_transaction)
    async def on_start_tx(self, connector_id, id_tag, meter_start, **kwargs):
        st = STATE.get(self.id) or ChargePointState(id=self.id)
        STATE[self.id] = st
        st.tx_active = True
        st.session_start_at = datetime.now(timezone.utc)
        try:
            st.session_start_kwh_reg = max(0.0, float(meter_start) / 1000.0)  # Wh -> kWh
        except Exception:
            st.session_start_kwh_reg = st.energy_kwh_total
        st.session_kwh = 0.0
        st.session_id = 1  # Demo
        return call_result.StartTransaction(transaction_id=st.session_id or 1, id_tag_info={"status": "Accepted"})

    @on(Action.stop_transaction)
    async def on_stop_tx(self, meter_stop, **kwargs):
        st = STATE.get(self.id)
        if st:
            try:
                stop_kwh = max(0.0, float(meter_stop) / 1000.0)
                if st.session_start_kwh_reg is not None:
                    st.session_kwh = round(max(0.0, stop_kwh - st.session_start_kwh_reg), 3)
            except Exception:
                pass
            st.tx_active = False
            st.session_est_end_at = None
        return call_result.StopTransaction()

    @on(Action.data_transfer)
    async def on_data_transfer(self, vendor_id, message_id=None, data=None, **kwargs):
        logger.info("DataTransfer from %s: vendor=%s message=%s", self.id, vendor_id, message_id)
        return call_result.DataTransfer(status=DataTransferStatus.accepted, data=data or "")

    @on(Action.meter_values)
    async def on_meter(self, meter_value, connector_id, **kwargs):
        """
        Liest SoC, Energie-Register, Leistung. Fallbacks:
         - schätzt Leistung aus Energie-Delta
         - schätzt Leistung aus Strom/Spannung, falls vorhanden
        """
        try:
            soc_found = None
            energy_kwh = None
            power_kw = None
            amps_sum = 0.0
            amps_found = False
            voltage_v = None
            ts = datetime.now(timezone.utc)

            for mv in (meter_value or []):
                ts_iso = mv.get("timestamp")
                if ts_iso:
                    try:
                        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
                    except Exception:
                        pass

                # "sampled_value" (snake) oder "sampledValue" (camel)
                for sv in mv.get("sampled_value", []) or mv.get("sampledValue", []):
                    meas = (sv.get("measurand") or sv.get("measured_value") or "").lower()
                    unit = (sv.get("unit") or "").lower()
                    val = sv.get("value")
                    v = _try_float(val)
                    if v is None:
                        continue

                    # SoC
                    if meas in ["soc", "stateofcharge", "state_of_charge"] or (not meas and unit in ["percent", "%"]):
                        soc_found = max(0, min(100, int(v)))

                    # Energie-Register
                    if meas in ["energy.active.import.register", "energy.active.import"] or ("energy" in meas and "import" in meas):
                        energy_kwh = max(0.0, v / 1000.0) if unit == "wh" else max(0.0, v)

                    # Leistung direkt
                    if meas in ["power.active.import", "power"] or ("power" in meas and "import" in meas):
                        power_kw = max(0.0, v / 1000.0) if unit in ["w", "watt"] else max(0.0, v)

                    # Strom/Spannung für Fallback
                    if meas.startswith("current") and "import" in meas:
                        amps_sum += float(v)
                        amps_found = True
                    if meas.startswith("voltage") or unit in ["v", "volt"]:
                        voltage_v = float(v)

            st = STATE.get(self.id)
            if st:
                if soc_found is not None:
                    st.current_soc = soc_found
                    st.soc = soc_found

                if energy_kwh is not None:
                    st.energy_kwh_total = energy_kwh
                    ENERGY_LOGS.setdefault(self.id, []).append((ts, energy_kwh))
                    logs = ENERGY_LOGS[self.id]
                    if len(logs) > 5000:
                        ENERGY_LOGS[self.id] = logs[-4000:]
                    # Session-kWh live
                    if st.tx_active and st.session_start_kwh_reg is not None:
                        st.session_kwh = round(max(0.0, energy_kwh - st.session_start_kwh_reg), 3)
                    # Leistung aus Delta schätzen, wenn nicht vorhanden
                    if power_kw is None and len(ENERGY_LOGS[self.id]) >= 2:
                        t2, e2 = ENERGY_LOGS[self.id][-1]
                        t1, e1 = ENERGY_LOGS[self.id][-2]
                        dt_h = max(1e-6, (t2 - t1).total_seconds() / 3600.0)
                        de_kwh = max(0.0, e2 - e1)
                        power_kw = de_kwh / dt_h

                # Fallback: aus Strom/Spannung
                if power_kw is None and amps_found:
                    U = float(voltage_v if voltage_v else (st.voltage_per_phase if st else 230.0))
                    power_kw = max(0.0, (U * amps_sum) / 1000.0)

                if power_kw is not None:
                    st.current_kw = round(power_kw, 2)

            if soc_found is not None:
                logger.info("MeterValues SoC %s -> %s%%", self.id, soc_found)
            if energy_kwh is not None:
                logger.info("MeterValues Energy %s -> %.3f kWh (session=%.3f)", self.id, energy_kwh, (st.session_kwh or 0.0) if st else 0.0)
            if power_kw is not None:
                logger.info("MeterValues Power %s -> %.2f kW", self.id, power_kw)

        except Exception as e:
            logger.exception("Error parsing MeterValues for %s: %s", self.id, e)

        return call_result.MeterValues()

    @on(Action.set_charging_profile)
    async def on_set_profile(self, connector_id=None, cs_charging_profiles=None, charging_profile=None, **kwargs):
        # Simulator/Tests nutzen diesen Pfad; bestätige sauber.
        return call_result.SetChargingProfile(status="Accepted")

    async def push_charging_profile(self, target_kw: float):
        """
        CS->CP: absolutes TxProfile mit Ampere-Limit passend zu target_kw.
        limit wird auf 0,1 A gerundet, um das OCPP-Schema (multipleOf 0.1) zu erfüllen.
        """
        st = STATE[self.id]
        target_kw = max(MIN_KW, min(MAX_KW, float(target_kw)))
        amps_raw = amps_from_kw(target_kw, st.phase_count, st.voltage_per_phase)
        amps = _round_to_0p1(amps_raw)

        period = ChargingSchedulePeriod(start_period=0, limit=float(amps))
        try:
            unit = ChargingRateUnitType.A
        except Exception:
            unit = "A"

        schedule = ChargingSchedule(
            charging_rate_unit=unit,
            charging_schedule_period=[period],
            duration=3600,
        )
        profile = ChargingProfile(
            charging_profile_id=1,
            stack_level=0,
            charging_profile_purpose=ChargingProfilePurposeType.tx_profile,
            charging_profile_kind=ChargingProfileKindType.absolute,
            charging_schedule=schedule,
        )
        req = call.SetChargingProfile(connector_id=1, cs_charging_profiles=profile)
        logger.info("Set profile %s -> %.2f kW (%.1f A; raw=%.6f A)", self.id, target_kw, amps, amps_raw)
        return await self.call(req)
