import asyncio, aiohttp, logging, os
from typing import Tuple
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from models import STATE

log = logging.getLogger(__name__)

# feste Schwellen für Eco-Mapping (nicht im UI konfigurierbar)
RAD_CLOUDY = 200.0  # W/m²
RAD_SUNNY  = 650.0  # W/m²

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

async def fetch_radiation(lat: float, lon: float) -> Tuple[float, float]:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=shortwave_radiation&forecast_days=1&timezone=auto"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=10) as r:
            j = await r.json()
    hours = j["hourly"]["time"]
    rad = j["hourly"]["shortwave_radiation"]
    from datetime import datetime as dt
    now_key = dt.now().strftime("%Y-%m-%dT%H:00")
    try:
        idx = hours.index(now_key)
    except ValueError:
        idx = 0
    cur = float(rad[idx])
    nxt = float(rad[min(idx + 1, len(rad) - 1)])
    avg = (cur + nxt) / 2.0
    return avg, cur

def eco_kw_from_radiation(avg_wm2: float, sunny_kw: float, cloudy_kw: float) -> float:
    if avg_wm2 <= RAD_CLOUDY:
        return cloudy_kw
    if avg_wm2 >= RAD_SUNNY:
        return sunny_kw
    t = (avg_wm2 - RAD_CLOUDY) / (RAD_SUNNY - RAD_CLOUDY)
    return cloudy_kw + t * (sunny_kw - cloudy_kw)

def next_dt(local_hhmm: str, tzname: str) -> datetime:
    tz = ZoneInfo(tzname)
    now = datetime.now(tz)
    hh, mm = [int(x) for x in local_hhmm.split(":")]
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate

async def control_loop(app, lat: float, lon: float, base_limit_kw: float):
    tzname = os.getenv("LOCAL_TZ", "Europe/Berlin")
    battery_kwh = float(os.getenv("BATTERY_KWH", "60"))
    efficiency  = float(os.getenv("EFFICIENCY", "0.92"))
    while True:
        try:
            avg_wm2, _ = await fetch_radiation(lat, lon)
            eco_cfg = app.state.eco  # {'sunny_kw': ..., 'cloudy_kw': ...}
            eco_kw = eco_kw_from_radiation(avg_wm2, eco_cfg["sunny_kw"], eco_cfg["cloudy_kw"])
            eco_kw = clamp(eco_kw, 0.0, base_limit_kw)

            for cp_id, st in STATE.items():
                if st.mode == "off":
                    target = 0.0

                elif st.mode == "max":
                    target = base_limit_kw

                else:  # eco
                    target = eco_kw
                    if st.boost_enabled:
                        cutoff = next_dt(st.boost_cutoff_local, tzname)
                        now_local = datetime.now(ZoneInfo(tzname))
                        hours_left = max(0.0, (cutoff - now_local).total_seconds() / 3600.0)
                        if hours_left > 0.0:
                            soc_now = float(st.current_soc if st.current_soc is not None else (st.soc or 0))
                            need_soc = max(0.0, st.boost_target_soc - soc_now)
                            need_kwh = (need_soc / 100.0) * battery_kwh
                            req_kw = (need_kwh / hours_left) / max(0.5, min(1.0, efficiency)) if need_kwh > 0 else 0.0
                            target = max(target, req_kw)  # Boost hebt Eco an, deckelt später
                        # Nach der Deadline KEIN hartes Abschalten: wir bleiben im Eco-Wert

                target = clamp(target, 0.0, base_limit_kw)
                st.target_kw = round(target, 2)
                cp = app.state.cps.get(cp_id)
                if cp:
                    await cp.push_charging_profile(st.target_kw)

            await asyncio.sleep(900)
        except Exception as e:
            log.exception("control loop error: %s", e)
            await asyncio.sleep(30)
