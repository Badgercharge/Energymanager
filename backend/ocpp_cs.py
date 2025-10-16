import logging
from datetime import datetime, timezone
from ocpp.routing import on
from ocpp.v16 import ChargePoint as CP, call_result, call
from ocpp.v16.enums import (
    RegistrationStatus, Action, ChargingRateUnitType,
    ChargingProfilePurposeType, ChargingProfileKindType,
)
from ocpp.v16.datatypes import ChargingSchedule, ChargingSchedulePeriod, ChargingProfile
from models import STATE, ChargePointState, ENERGY_LOGS

logger = logging.getLogger(__name__)

MIN_KW = 3.7
MAX_KW = 11.0

def amps_from_kw(kw: float, phases: int, voltage: float) -> float:
    kw = max(MIN_KW, min(MAX_KW, kw))
    return max(0.0, kw * 1000.0 / (max(1.0, voltage) * max(1, phases)))

def _try_float(x):
    try: return float(x)
    except: return None

class CentralSystem(CP):
    @on(Action.boot_notification)
    async def on_boot(self, charge_point_model, charge_point_vendor, **kwargs):
        cp_id = self.id
        STATE.setdefault(cp_id, ChargePointState(id=cp_id, connected=True))
        logger.info("Boot from %s (%s/%s)", cp_id, charge_point_vendor, charge_point_model)
        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=30,
            status=RegistrationStatus.accepted,
        )

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
            # Defaults bei Start eines Ladevorgangs
            if str(status).lower() == "charging":
                st.mode = "eco"
                st.boost_enabled = True
                st.boost_cutoff_local = "07:00"
                st.boost_target_soc = 100
                st.boost_reached_notified = False
                st.tx_active = True
            if str(status).lower() in ["available", "finishing", "suspendedev", "suspendedevse"]:
                # nicht hart auf False setzen – StopTransaction macht das sicher
                pass
        return call_result.StatusNotification()

    @on(Action.start_transaction)
    async def on_start_tx(self, connector_id, id_tag, meter_start, **kwargs):
        st = STATE.get(self.id) or ChargePointState(id=self.id)
        STATE[self.id] = st
        st.mode = "eco"
        st.boost_enabled = True
        st.boost_cutoff_local = "07:00"
        st.boost_target_soc = 100
        st.boost_reached_notified = False
        st.tx_active = True
        st.session_start_at = datetime.now(timezone.utc)
        # OCPP MeterStart ist typ. Wh
        try:
            st.session_start_kwh_reg = max(0.0, float(meter_start) / 1000.0)
        except Exception:
            st.session_start_kwh_reg = st.energy_kwh_total  # Fallback
        st.session_kwh = 0.0
        st.session_id = 1  # Dummy; echte Boxen liefern transactionId im CallResult
        return call_result.StartTransaction(transaction_id=st.session_id or 1, id_tag_info={"status":"Accepted"})

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

    @on(Action.meter_values)
    async def on_meter(self, meter_value, connector_id, **kwargs):
        try:
            soc_found = None
            energy_kwh = None
            power_kw = None
            ts = datetime.now(timezone.utc)

            for mv in (meter_value or []):
                ts_iso = mv.get("timestamp")
                if ts_iso:
                    try:
                        ts = datetime.fromisoformat(ts_iso.replace("Z","+00:00"))
                    except:
                        pass
                for sv in mv.get("sampled_value", []) or mv.get("sampledValue", []):
                    meas = (sv.get("measurand") or sv.get("measured_value") or "").lower()
                    unit = (sv.get("unit") or "").lower()
                    val  = sv.get("value")

                    if meas in ["soc","stateofcharge","state_of_charge"] or (not meas and unit in ["percent","%"]):
                        v = _try_float(val)
                        if v is not None:
                            soc_found = max(0, min(100, int(v)))

                    if meas in ["energy.active.import.register","energy.active.import"] or ("energy" in meas and "import" in meas):
                        v = _try_float(val)
                        if v is not None:
                            energy_kwh = max(0.0, v/1000.0) if unit == "wh" else max(0.0, v)

                    if meas in ["power.active.import","power"] or ("power" in meas and "import" in meas):
                        v = _try_float(val)
                        if v is not None:
                            power_kw = max(0.0, v/1000.0) if unit in ["w","watt"] else max(0.0, v)

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
                    # Session-kWh live berechnen
                    if st.tx_active and st.session_start_kwh_reg is not None:
                        st.session_kwh = round(max(0.0, energy_kwh - st.session_start_kwh_reg), 3)
                    # Falls keine Leistung geliefert: aus Energie-Delta schätzen
                    if power_kw is None and len(ENERGY_LOGS[self.id]) >= 2:
                        t2, e2 = ENERGY_LOGS[self.id][-1]
                        t1, e1 = ENERGY_LOGS[self.id][-2]
                        dt_h = max(1e-6, (t2 - t1).total_seconds() / 3600.0)
                        de_kwh = max(0.0, e2 - e1)
                        power_kw = de_kwh / dt_h

                if power_kw is not None:
                    st.current_kw = round(power_kw, 2)

            if soc_found is not None:
                logger.info("MeterValues SoC %s -> %s%%", self.id, soc_found)
            if energy_kwh is not None:
                logger.info("MeterValues Energy %s -> %.3f kWh (session=%.3f)", self.id, energy_kwh, st.session_kwh or 0.0)
            if power_kw is not None:
                logger.info("MeterValues Power %s -> %.2f kW", self.id, power_kw)

        except Exception as e:
            logger.exception("Error parsing MeterValues for %s: %s", self.id, e)

        return call_result.MeterValues()

    async def push_charging_profile(self, target_kw: float):
        st = STATE[self.id]
        target_kw = max(MIN_KW, min(MAX_KW, target_kw))
        amps = amps_from_kw(target_kw, st.phase_count, st.voltage_per_phase)
        period = ChargingSchedulePeriod(start_period=0, limit=float(amps))
        schedule = ChargingSchedule(
            charging_rate_unit=ChargingRateUnitType.A,  # wichtig: Großes A
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
        logger.info("Set profile %s -> %.2f kW (%.1f A)", self.id, target_kw, amps)
        return await self.call(req)
