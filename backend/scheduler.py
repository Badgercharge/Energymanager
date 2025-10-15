import asyncio, aiohttp, logging, os
from models import STATE
from typing import Tuple

log = logging.getLogger(__name__)

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

async def fetch_radiation(lat: float, lon: float) -> Tuple[float, float]:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=shortwave_radiation&forecast_days=1&timezone=auto"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=10) as r:
            j = await r.json()
    hours = j["hourly"]["time"]
    rad = j["hourly"]["shortwave_radiation"]  # W/m²
    from datetime import datetime as dt
    now_key = dt.now().strftime("%Y-%m-%dT%H:00")
    try:
        idx = hours.index(now_key)
    except ValueError:
        idx = 0
    cur_wm2 = float(rad[idx])
    nxt_wm2 = float(rad[min(idx+1, len(rad)-1)])
    avg_wm2 = (cur_wm2 + nxt_wm2) / 2.0
    return avg_wm2, cur_wm2

def target_kw_from_radiation(avg_wm2: float) -> float:
    sunny_kw = float(os.getenv("SUNNY_KW", "11.0"))
    cloudy_kw = float(os.getenv("CLOUDY_KW", "3.7"))
    rad_sunny = float(os.getenv("RAD_SUNNY", "650"))    # W/m²
    rad_cloudy = float(os.getenv("RAD_CLOUDY", "200"))  # W/m²
    if avg_wm2 <= rad_cloudy:
        return cloudy_kw
    if avg_wm2 >= rad_sunny:
        return sunny_kw
    # Linear interpolation between cloudy and sunny bands
    t = (avg_wm2 - rad_cloudy) / (rad_sunny - rad_cloudy)
    return cloudy_kw + t * (sunny_kw - cloudy_kw)

async def control_loop(app, lat: float, lon: float, base_limit_kw: float, min_grid_kw: float):
    while True:
        try:
            avg_wm2, _ = await fetch_radiation(lat, lon)
            dyn_kw = target_kw_from_radiation(avg_wm2)
            for cp_id, st in STATE.items():
                if st.mode == "off":
                    target = 0.0
                elif st.mode == "max":
                    target = base_limit_kw
                else:  # eco
                    # Im Eco-Modus Zielwert aus Strahlung ableiten, auf Base-Limit deckeln
                    target = clamp(dyn_kw, 0.0, base_limit_kw)
                st.target_kw = round(target, 2)
                cp = app.state.cps.get(cp_id)
                if cp:
                    await cp.push_charging_profile(st.target_kw)
            await asyncio.sleep(900)  # alle 15 Min
        except Exception as e:
            log.exception("control loop error: %s", e)
            await asyncio.sleep(30)
