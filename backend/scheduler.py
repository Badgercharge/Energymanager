# backend/scheduler.py
import asyncio
import aiohttp
import logging
import os
from typing import Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from models import STATE
from mailer import send_mail, fmt_ts
from price_provider import fetch_prices_ct_per_kwh, median

log = logging.getLogger(__name__)

# Feste Leistungsgrenzen (kW)
MIN_KW = 3.7
MAX_KW = 11.0

# Heuristik für Eco (Globalstrahlung W/m²)
RAD_CLOUDY = 200.0
RAD_SUNNY  = 650.0


def clamp_kw(x: float) -> float:
    return max(MIN_KW, min(MAX_KW, float(x)))


async def fetch_radiation(lat: float, lon: float) -> Tuple[float, float]:
    """
    Holt aktuelle und nächste Stunde Kurzwelleneinstrahlung von Open-Meteo
    und gibt den Mittelwert der beiden (avg) sowie den aktuellen Wert zurück.
    """
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
    """
    Linear zwischen CLOUDY_KW und SUNNY_KW interpolieren.
    """
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


async def weather_loop(app, lat: float, lon: float, tzname: str):
    """
    Aktualisiert alle 5 Minuten das aktuelle Wetter in app.state.weather.
    """
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
        await asyncio.sleep(300)  # 5 Minuten


async def control_loop(app, lat: float, lon: float, base_limit_kw: float):
    """
    Haupt-Regelschleife (alle 15 Minuten):
    - Aktualisiert Preisstatus (current, median) für /api/price
    - Berechnet Ziel-Leistung je Ladepunkt je nach Modus (manual > off/max/price/eco)
    - Schätzt voraussichtliches Ende der Session (session_est_end_at) bei vorhandenem SoC
    - Schiebt SetChargingProfile an die Wallbox
    """
    tzname = os.getenv("LOCAL_TZ", "Europe/Berlin")
    battery_kwh = float(os.getenv("BATTERY_KWH", "60"))
    efficiency = float(os.getenv("EFFICIENCY", "0.92"))

    # Preis-Cache
    prices_cache = {"ts": None, "series": [], "median": None, "cur": None}

    # Pricingobjekt initialisieren
    if not hasattr(app.state, "pricing"):
        app.state.pricing = {
            "as_of": None,
            "current_ct_per_kwh": None,
            "median_ct_per_kwh": None,
            "below_or_equal_median": None,
        }

    while True:
        try:
            # Eco-Grundlage aus Globalstrahlung
            avg_wm2, _ = await fetch_radiation(lat, lon)
            eco_cfg = app.state.eco  # {"sunny_kw":..., "cloudy_kw":...}
            eco_kw = clamp_kw(eco_kw_from_radiation(avg_wm2, eco_cfg["sunny_kw"], eco_cfg["cloudy_kw"]))
            base_limit_kw = clamp_kw(base_limit_kw)

            # Preise alle ~10 Minuten laden
            now_local = datetime.now(ZoneInfo(tzname))
            if (prices_cache["ts"] is None) or ((now_local - prices_cache["ts"]).total_seconds() > 600):
                series = await fetch_prices_ct_per_kwh(now_local)
                prices_cache["series"] = series
                today_prices = [p for ts, p in series if ts.astimezone(ZoneInfo(tzname)).date() == now_local.date()]
                prices_cache["median"] = median(today_prices)
                prices_cache["ts"] = now_local

            # aktuellen Preis: letzter Punkt <= now_utc
            cur_price = None
            if prices_cache["series"]:
                now_utc = now_local.astimezone(timezone.utc)
                for ts, price in prices_cache["series"]:
                    if ts <= now_utc:
                        cur_price = price
                    else:
                        break
            prices_cache["cur"] = cur_price

            # API-Status für /api/price
            app.state.pricing = {
                "as_of": now_local.isoformat(),
                "current_ct_per_kwh": cur_price,
                "median_ct_per_kwh": prices_cache["median"],
                "below_or_equal_median": (
                    cur_price is not None
                    and prices_cache["median"] is not None
                    and cur_price <= prices_cache["median"]
                ),
            }

            # Regelung je Ladepunkt
            for cp_id, st in STATE.items():
                # 1) Ziel-Leistung je Modus (manual hat Vorrang)
                if st.mode == "manual":
                    target = st.target_kw

                elif st.mode == "off":
                    # Untere Grenze – wenn du stattdessen 0 A willst, sag Bescheid.
                    target = MIN_KW

                elif st.mode == "max":
                    target = MAX_KW

                elif st.mode == "price":
                    # Preisgesteuert: <= Median -> MAX, sonst MIN
                    if cur_price is not None and prices_cache["median"] is not None:
                        target = MAX_KW if cur_price <= prices_cache["median"] else MIN_KW
                    else:
                        target = MIN_KW
                    # 100% bis 07:00 absichern
                    cutoff = next_dt("07:00", tzname)
                    hours_left = max(0.0, (cutoff - now_local).total_seconds() / 3600.0)
                    if hours_left > 0.0:
                        soc_now = float(st.current_soc if st.current_soc is not None else (st.soc or 0))
                        need_soc = max(0.0, 100 - soc_now)
                        need_kwh = (need_soc / 100.0) * battery_kwh
                        eff = max(0.5, min(1.0, efficiency))
                        req_kw = (need_kwh / hours_left) / eff if need_kwh > 0 else 0.0
                        target = max(target, req_kw)

                else:  # "eco"
                    target = eco_kw
                    if st.boost_enabled:
                        cutoff = next_dt(st.boost_cutoff_local, tzname)
                        hours_left = max(0.0, (cutoff - now_local).total_seconds() / 3600.0)
                        # Ziel erreicht? E-Mail einmalig
                        if st.current_soc is not None and st.current_soc >= st.boost_target_soc and not st.boost_reached_notified:
                            st.boost_reached_notified = True
                            asyncio.create_task(
                                send_mail(
                                    f"[EMS] Ziel-SoC erreicht – {cp_id}",
                                    f"Ladepunkt: {cp_id}\nSoC: {st.current_soc}% (Ziel {st.boost_target_soc}%)\nZeit: {fmt_ts()}\n",
                                )
                            )
                        if hours_left > 0.0:
                            soc_now = float(st.current_soc if st.current_soc is not None else (st.soc or 0))
                            need_soc = max(0.0, st.boost_target_soc - soc_now)
                            need_kwh = (need_soc / 100.0) * battery_kwh
                            eff = max(0.5, min(1.0, efficiency))
                            req_kw = (need_kwh / hours_left) / eff if need_kwh > 0 else 0.0
                            target = max(target, req_kw)

                target = clamp_kw(target)
                st.target_kw = round(target, 2)

                # 2) Prognose: voraussichtliches Ende (nur wenn Session aktiv und SoC bekannt)
                st.session_est_end_at = None
                if getattr(st, "tx_active", False) and (st.current_soc is not None):
                    target_soc = st.boost_target_soc if (st.mode == "eco" and st.boost_enabled) else 100
                    need_soc = max(0.0, target_soc - float(st.current_soc))
                    if need_soc > 0 and target > 0:
                        need_kwh = (need_soc / 100.0) * battery_kwh
                        eff = max(0.5, min(1.0, efficiency))
                        hours = need_kwh / (target * eff)
                        st.session_est_end_at = now_local + timedelta(hours=hours)

                # 3) SetChargingProfile senden
                cp = app.state.cps.get(cp_id)
                if cp:
                    await cp.push_charging_profile(st.target_kw)

            # Haupt-Takt: 15 Minuten
            await asyncio.sleep(900)

        except Exception as e:
            log.exception("control loop error: %s", e)
            await asyncio.sleep(30)
