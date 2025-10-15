import logging
from ocpp.routing import on
from ocpp.v16 import ChargePoint as CP, call_result, call
from ocpp.v16.enums import RegistrationStatus, Action, ChargingRateUnitType, ChargingProfilePurposeType, ChargingProfileKindType
from ocpp.v16.datatypes import ChargingSchedule, ChargingSchedulePeriod, ChargingProfile
from datetime import datetime, timezone
from models import STATE, ChargePointState

logger = logging.getLogger(__name__)

def amps_from_kw(kw: float, phases: int, voltage: float) -> float:
    # Näherung P ≈ U * I * Phasen (Haushalt)
    return max(0.0, kw * 1000.0 / (voltage * phases))

class CentralSystem(CP):
    @on(Action.BootNotification)
    async def on_boot(self, charge_point_model, charge_point_vendor, **kwargs):
        cp_id = self.id
        STATE.setdefault(cp_id, ChargePointState(id=cp_id, connected=True))
        logger.info("Boot from %s (%s/%s)", cp_id, charge_point_vendor, charge_point_model)
        return call_result.BootNotificationPayload(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=30,
            status=RegistrationStatus.accepted,
        )

    @on(Action.Heartbeat)
    async def on_heartbeat(self):
        STATE[self.id].connected = True
        STATE[self.id].last_heartbeat = datetime.now(timezone.utc)
        return call_result.HeartbeatPayload(current_time=datetime.now(timezone.utc).isoformat())

    @on(Action.StatusNotification)
    async def on_status(self, **kwargs):
        STATE[self.id].connected = True
        return call_result.StatusNotificationPayload()

    @on(Action.MeterValues)
    async def on_meter(self, **kwargs):
        return call_result.MeterValuesPayload()

    async def push_charging_profile(self, target_kw: float):
        st = STATE[self.id]
        amps = amps_from_kw(target_kw, st.phase_count, st.voltage_per_phase)
        period = ChargingSchedulePeriod(start_period=0, limit=amps)
        schedule = ChargingSchedule(
            charging_rate_unit=ChargingRateUnitType.a,
            charging_schedule_period=[period],
            duration=3600
        )
        profile = ChargingProfile(
            charging_profile_id=1,
            stack_level=0,
            charging_profile_purpose=ChargingProfilePurposeType.tx_profile,
            charging_profile_kind=ChargingProfileKindType.absolute,
            charging_schedule=schedule
        )
        req = call.SetChargingProfilePayload(
            connector_id=1,
            cs_charging_profiles=profile
        )
        logger.info("Set profile %s -> %.2f kW (%.1f A)", self.id, target_kw, amps)
        return await self.call(req)
