import asyncio
import aiohttp
import logging
import os
from typing import Tuple, Dict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from models import STATE
from mailer import send_mail, fmt_ts
from price_provider import fetch_prices_ct_per_kwh, median

log = logging.getLogger(__name__)

MIN_KW = 3.7
MAX_KW = 11.0

RAD_CLOUDY = 200.0
RAD_SUNNY  = 650.0

# Tuning via Env
LOOP_SECONDS            = int(os.getenv("CTRL_LOOP_SECONDS", "60"))
PRICE_REFRESH_SECONDS   = int(os.getenv("PRICE_REFRESH_SECONDS", "300"))
WEATHER_REFRESH_SECONDS = int(os.getenv("WEATHER_REFRESH_SECONDS", "120"))
PUSH_DEADBAND_KW        = float(os.getenv("PUSH_DEADBAND_KW", "0.1"))

def clamp_kw(x: float) -> float:
    return max(MIN_KW, min(MAX_KW, float(x)))

async def fetch_radiation(lat: float, lon: float) -> Tuple[float, float]:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=shortwave_radiation&forecast_days=1&timezone=auto"
    )
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
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand

def seconds_to_next_quarter(now: datetime) -> int:
    minute = (now.minute // 15 + 1) * 15
    next_q = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    return int((next_q - now).total_seconds())

async def weather_loop(app, lat: float, lon: float, tzname: str):
    app.state.weather = {"as_of": None}
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,cloud_cover,shortwave_radiation,wind_speed_10m,precipitation"
        "&timezone=auto"
    )
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=10) as r:
                    j = await r.json()
            cur = (j.get("current") or {})
            app.state.weather = {
                "as_of": datetime.now(ZoneInfo(tzname)).isoformat(),
                "temperature_c": cur.get("temperature_2m"),
                "cloud_cover_pct": cur.get("cloud_cover"),
                "shortwave_radiation_wm2": cur.get("shortwave_radiation"),
                "wind_speed_ms": cur.get("wind_speed_10m"),
                "precip_mm": cur.get("precipitation"),
            }
        except Exception as e:
            log.warning("weather fetch failed: %s", e)
        await asyncio.sleep(WEATHER_REFRESH_SECONDS)

async def control_loop(app, lat: float, lon: float, base_limit_kw: float):
    tzname = os.getenv("LOCAL_TZ", "Europe/Berlin")
    battery_kwh = float(os.getenv("BATTERY_KWH", "60"))
    efficiency  = float(os.getenv("EFFICIENCY", "0.92"))

    prices_cache = {"ts": None, "series": [], "median": None, "cur": None}
    last_push_kw: Dict[str, float] = {}

    if not hasattr(app.state, "pricing"):
        app.state.pricing = {
            "as_of": None,
            "current_ct_per_kwh": None,
            "median_ct_per_kwh": None,
            "below_or_equal_median": None,
        }

    async def refresh_prices(now_local, force=False):
        need = False
        if force or prices_cache["ts"] is None:
            need = True
        elif (now_local - prices_cache["ts"]).total_seconds() >= PRICE_REFRESH_SECONDS:
            need = True
        else:
            s_to_next = seconds_to_next_quarter(now_local)
            if s_to_next <= 90 or (900 - s_to_next) <= 90:
                need = True
        if not need:
            return

        series = await fetch_prices_ct_per_kwh(now_local)
        prices_cache["series"] = series
        prices_cache["ts"] = now_local

        cur = None
        if series:
            now_utc = now_local.astimezone(timezone.utc)
            for ts, price in series:
                if ts <= now_utc:
                    cur = price
                else:
                    break
        today_vals = [p for ts, p in series if ts.astimezone(ZoneInfo(tzname)).date() == now_local.date()]
        med = median(today_vals) if today_vals else None

        prices_cache["cur"] = cur
        prices_cache["median"] = med

        app.state.pricing = {
            "as_of": now_local.isoformat(),
            "current_ct_per_kwh": cur,
            "median_ct_per_kwh": med,
            "below_or_equal_median": (cur is not None and med is not None and cur <= med),
        }

    while True:
        try:
            now_local = datetime.now(ZoneInfo(tzname))

            # 1) Eco-Basis aus Strahlung
            try:
                avg_wm2, _ = await fetch_radiation(lat, lon)
            except Exception as e:
                log.warning("radiation fetch failed: %s", e)
                avg_wm2 = RAD_CLOUDY
            eco_cfg = app.state.eco
            eco_kw = clamp_kw(eco_kw_from_radiation(avg_wm2, eco_cfg["sunny_kw"], eco_cfg["cloudy_kw"]))
            base_limit_kw = clamp_kw(base_limit_kw)

            # 2) Preise
            await refresh_prices(now_local)
            cur_price = prices_cache["cur"]
            median_today = prices_cache["median"]

            # 3) Je Ladepunkt regeln
            for cp_id, st in STATE.items():
                if st.mode == "manual":
                    target = st.target_kw

                elif st.mode == "off":
                    target = MIN_KW  # auf Wunsch auf 0.0 ändern

                elif st.mode == "max":
                    target = MAX_KW

                elif st.mode == "price":
                    if cur_price is not None and median_today is not None:
                        target = MAX_KW if cur_price <= median_today else MIN_KW
                    else:
                        target = MIN_KW
                    # BOOST-Overlay (nur Price/Eco)
                    if st.boost_enabled:
                        cutoff = next_dt(st.boost_cutoff_local, tzname)
                        target_soc = st.boost_target_soc
                    else:
                        cutoff = next_dt("07:00", tzname)
                        target_soc = 100
                    hours_left = max(0.0, (cutoff - now_local).total_seconds() / 3600.0)
                    if hours_left > 0.0:
                        soc_now = float(st.current_soc if st.current_soc is not None else (st.soc or 0))
                        need_soc = max(0.0, target_soc - soc_now)
                        need_kwh = (need_soc / 100.0) * battery_kwh
                        eff = max(0.5, min(1.0, efficiency))
                        req_kw = (need_kwh / hours_left) / eff if need_kwh > 0 else 0.0
                        target = max(target, req_kw)

                else:  # eco
                    target = eco_kw
                    if st.boost_enabled:
                        cutoff = next_dt(st.boost_cutoff_local, tzname)
                        hours_left = max(0.0, (cutoff - now_local).total_seconds() / 3600.0)
                        if st.current_soc is not None and st.current_soc >= st.boost_target_soc and not st.boost_reached_notified:
                            st.boost_reached_notified = True
                            asyncio.create_task(send_mail(
                                f"[EMS] Ziel-SoC erreicht – {cp_id}",
                                f"Ladepunkt: {cp_id}\nSoC: {st.current_soc}% (Ziel {st.boost_target_soc}%)\nZeit: {fmt_ts()}\n"
                            ))
                        if hours_left > 0.0:
                            soc_now = float(st.current_soc if st.current_soc is not None else (st.soc or 0))
                            need_soc = max(0.0, st.boost_target_soc - soc_now)
                            need_kwh = (need_soc / 100.0) * battery_kwh
                            eff = max(0.5, min(1.0, efficiency))
                            req_kw = (need_kwh / hours_left) / eff if need_kwh > 0 else 0.0
                            target = max(target, req_kw)

                target = clamp_kw(target)
                st.target_kw = round(target, 2)

                # Ende prognostizieren
                st.session_est_end_at = None
                if getattr(st, "tx_active", False) and (st.current_soc is not None):
                    target_soc = st.boost_target_soc if (st.mode == "eco" and st.boost_enabled) else 100
                    need_soc = max(0.0, target_soc - float(st.current_soc))
                    if need_soc > 0 and target > 0:
                        need_kwh = (need_soc / 100.0) * battery_kwh
                        eff = max(0.5, min(1.0, efficiency))
                        hours = need_kwh / (target * eff)
                        st.session_est_end_at = now_local + timedelta(hours=hours)

                # Nur pushen, wenn sich Ziel ausreichend geändert hat
                last = last_push_kw.get(cp_id)
                if last is None or abs(st.target_kw - last) >= PUSH_DEADBAND_KW:
                    cp = app.state.cps.get(cp_id)
                    if cp:
                        try:
                            await cp.push_charging_profile(st.target_kw)
                            last_push_kw[cp_id] = st.target_kw
                        except Exception as e:
                            log.warning("push profile %s failed: %s", cp_id, e)

            # Sleep dynamisch (eng an Viertelstunden)
            sleep_s = LOOP_SECONDS
            s_to_next = seconds_to_next_quarter(now_local)
            if s_to_next <= 10 or (900 - s_to_next) <= 10:
                sleep_s = min(sleep_s, 5)
            await asyncio.sleep(max(1, sleep_s))

        except Exception as e:
            log.exception("control loop error: %s", e)
            await asyncio.sleep(10)
