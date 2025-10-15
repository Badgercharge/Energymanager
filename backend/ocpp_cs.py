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

def amps_from_kw(kw: float, phases: int, voltage: float) -> float:
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
        if self.id in STATE:
            STATE[self.id].connected = True
            STATE[self.id].last_heartbeat = datetime.now(timezone.utc)
        return call_result.Heartbeat(current_time=datetime.now(timezone.utc).isoformat())

    @on(Action.status_notification)
    async def on_status(self, **kwargs):
        if self.id in STATE:
            STATE[self.id].connected = True
        return call_result.StatusNotification()

    @on(Action.meter_values)
    async def on_meter(self, meter_value, connector_id, **kwargs):
        """
        Liest SoC (%), Energie-Zähler (kWh) aus MeterValues.
        Energie:
          - bevorzugt measurand 'Energy.Active.Import.Register'
          - unit Wh -> /1000
          - unit kWh -> direkt
        """
        try:
            soc_found = None
            energy_kwh = None
            ts = datetime.now(timezone.utc)

            for mv in (meter_value or []):
                # Timestamp (falls geliefert)
                ts_iso = mv.get("timestamp")
                if ts_iso:
                    try: ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
                    except: pass

                for sv in mv.get("sampled_value", []):
                    meas = (sv.get("measurand") or sv.get("measured_value") or "").lower()
                    unit = (sv.get("unit") or "").lower()
                    val  = sv.get("value")

                    # SoC
                    if meas in ["soc", "stateofcharge", "state_of_charge"] or (not meas and unit in ["percent", "%"]):
                        v = _try_float(val)
                        if v is not None:
                            soc_found = max(0, min(100, int(v)))

                    # Energie
                    if meas in ["energy.active.import.register", "energy.active.import"] or ("energy" in meas and "import" in meas):
                        v = _try_float(val)
                        if v is not None:
                            if unit in ["wh"]:
                                energy_kwh = max(0.0, v / 1000.0)
                            else:
                                # angenommen kWh oder unbekannt -> kWh
                                energy_kwh = max(0.0, v)

            st = STATE.get(self.id)
            if st:
                if soc_found is not None:
                    st.current_soc = soc_found
                    st.soc = soc_found
                if energy_kwh is not None:
                    st.energy_kwh_total = energy_kwh
                    ENERGY_LOGS.setdefault(self.id, []).append((ts, energy_kwh))
                    # prune: optional, um Größe zu begrenzen (z. B. > 60 Tage)
                    logs = ENERGY_LOGS[self.id]
                    if len(logs) > 5000:
                        ENERGY_LOGS[self.id] = logs[-4000:]
            if soc_found is not None:
                logger.info("MeterValues SoC %s -> %s%%", self.id, soc_found)
            if energy_kwh is not None:
                logger.info("MeterValues Energy %s -> %.3f kWh", self.id, energy_kwh)

        except Exception as e:
            logger.exception("Error parsing MeterValues for %s: %s", self.id, e)

        return call_result.MeterValues()

    async def push_charging_profile(self, target_kw: float):
        st = STATE[self.id]
        amps = amps_from_kw(target_kw, st.phase_count, st.voltage_per_phase)
        period = ChargingSchedulePeriod(start_period=0, limit=amps)
        schedule = ChargingSchedule(
            charging_rate_unit=ChargingRateUnitType.a,
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
