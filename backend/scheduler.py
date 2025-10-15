# backend/scheduler.py – ersetze control_loop durch die Version unten
import asyncio, aiohttp, logging, os
from models import STATE
from typing import Tuple
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

# ... (fetch_radiation und target_kw_from_radiation bleiben wie zuvor)

def next_cutoff_dt(local_time_hhmm: str, tzname: str) -> datetime:
    tz = ZoneInfo(tzname)
    now = datetime.now(tz)
    hh, mm = [int(x) for x in local_time_hhmm.split(":")]
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate

async def control_loop(app, lat: float, lon: float, base_limit_kw: float, min_grid_kw: float):
    tzname = os.getenv("LOCAL_TZ", "Europe/Berlin")
    while True:
        try:
            avg_wm2, _ = await fetch_radiation(lat, lon)
            eco_kw = clamp(target_kw_from_radiation(avg_wm2), 0.0, base_limit_kw)

            for cp_id, st in STATE.items():
                # Standard: aus
                target = 0.0

                if st.mode == "off":
                    target = 0.0

                elif st.mode == "max":
                    target = base_limit_kw

                elif st.mode in ("eco", "schedule"):
                    # Eco-Basis
                    target = eco_kw

                    if st.mode == "schedule" and st.schedule_enabled:
                        # Deadline und SoC berücksichtigen
                        cutoff = next_cutoff_dt(st.cutoff_local, tzname)
                        now_local = datetime.now(ZoneInfo(tzname))
                        hours_left = max(0.0, (cutoff - now_local).total_seconds() / 3600.0)

                        if hours_left <= 0.0:
                            # nach Deadline strikt aus
                            target = 0.0
                        else:
                            # benötigte Energie (kWh)
                            soc_now = float(st.current_soc or 0)
                            need_soc = max(0.0, st.target_soc - soc_now)
                            need_kwh = (need_soc / 100.0) * float(st.battery_kwh)
                            eff = max(0.5, min(1.0, float(st.charge_efficiency or 0.92)))
                            # erforderliche kW (inkl. Wirkungsgrad)
                            req_kw = (need_kwh / hours_left) / eff if need_kwh > 0 else 0.0
                            # mindestens Eco, höchstens Base-Limit
                            target = clamp(max(eco_kw, req_kw), 0.0, base_limit_kw)

                # Ergebnis setzen & pushen
                st.target_kw = round(target, 2)
                cp = app.state.cps.get(cp_id)
                if cp:
                    await cp.push_charging_profile(st.target_kw)

            await asyncio.sleep(900)  # alle 15 Minuten
        except Exception as e:
            log.exception("control loop error: %s", e)
            await asyncio.sleep(30)
