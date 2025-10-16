import asyncio, aiohttp, logging, os
from typing import Tuple
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

def clamp_kw(x: float) -> float:
    return max(MIN_KW, min(MAX_KW, x))

async def fetch_radiation(lat: float, lon: float) -> Tuple[float, float]:
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=shortwave_radiation&forecast_days=1&timezone=auto"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=10) as r:
            j = await r.json()
    hours = j["hourly"]["time"]
    rad = j["hourly"]["shortwave_radiation"]
    from datetime import datetime as dt
    now_key = dt.now().strftime("%Y-%m-%dT%H:00")
    try: idx = hours.index(now_key)
    except ValueError: idx = 0
    cur = float(rad[idx])
    nxt = float(rad[min(idx + 1, len(rad) - 1)])
    avg = (cur + nxt) / 2.0
    return avg, cur

def eco_kw_from_radiation(avg_wm2: float, sunny_kw: float, cloudy_kw: float) -> float:
    if avg_wm2 <= RAD_CLOUDY: return cloudy_kw
    if avg_wm2 >= RAD_SUNNY:  return sunny_kw
    t = (avg_wm2 - RAD_CLOUDY) / (RAD_SUNNY - RAD_CLOUDY)
    return cloudy_kw + t * (sunny_kw - cloudy_kw)

def next_dt(local_hhmm: str, tzname: str) -> datetime:
    tz = ZoneInfo(tzname)
    now = datetime.now(tz)
    hh, mm = [int(x) for x in local_hhmm.split(":")]
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now: cand += timedelta(days=1)
    return cand

async def _update_pricing(app, tzname: str):
    """
    Holt Preise, ermittelt aktuellen Slot und Median für den lokalen Tag
    und schreibt app.state.pricing. Nutzt aWATTar (stundenweise, auf PT15M expandiert)
    oder ENTSO-E (PT15M), je nach Konfiguration in price_provider.py.
    """
    now_local = datetime.now(ZoneInfo(tzname))
    now_utc = now_local.astimezone(timezone.utc)

    series = await fetch_prices_ct_per_kwh(now_local)
    if not series:
        app.state.pricing = {
            "as_of": now_local.isoformat(),
            "current_ct_per_kwh": None,
            "median_ct_per_kwh": None,
            "below_or_equal_median": None,
        }
        log.warning("pricing: no price series available (check PRICE_API_URL or network)")
        return

    # aktueller Preis = letzter Punkt mit ts <= now_utc
    cur_price = None
    for ts, price in series:
        if ts <= now_utc:
            cur_price = price
        else:
            break

    # Median über alle Punkte des lokalen Kalendertags
    todays = [p for ts, p in series if ts.astimezone(ZoneInfo(tzname)).date() == now_local.date()]
    med = median(todays) if todays else None

    app.state.pricing = {
        "as_of": now_local.isoformat(),
        "current_ct_per_kwh": cur_price,
        "median_ct_per_kwh": med,
        "below_or_equal_median": (cur_price is not None and med is not None and cur_price <= med),
    }

async def control_loop(app, lat: float, lon: float, base_limit_kw: float):
    tzname = os.getenv("LOCAL_TZ", "Europe/Berlin")
    battery_kwh = float(os.getenv("BATTERY_KWH", "60"))
    efficiency  = float(os.getenv("EFFICIENCY", "0.92"))

    # Pricing-Objekt initialisieren und sofort befüllen
    if not hasattr(app.state, "pricing"):
        app.state.pricing = {"as_of": None, "current_ct_per_kwh": None, "median_ct_per_kwh": None, "below_or_equal_median": None}
    await _update_pricing(app, tzname)

    # Hauptschleife (alle 15 Minuten)
    while True:
        try:
            # a) Wetter
            avg_wm2, _ = await fetch_radiation(lat, lon)
            eco_cfg = app.state.eco
            eco_kw = clamp_kw(eco_kw_from_radiation(avg_wm2, eco_cfg["sunny_kw"], eco_cfg["cloudy_kw"]))
            base_limit_kw = clamp_kw(base_limit_kw)

            # b) Preise: alle 5 Minuten refresh (damit median/current aktuell bleiben)
            try:
                await _update_pricing(app, tzname)
            except Exception as e:
                log.exception("pricing update failed: %s", e)

            cur_price = app.state.pricing.get("current_ct_per_kwh")
            med_price = app.state.pricing.get("median_ct_per_kwh")

            # c) Regelung je Ladepunkt
            for cp_id, st in STATE.items():
                if st.mode == "off":
                    target = MIN_KW  # untere Grenze; wenn du 0 A willst, sag Bescheid.
                elif st.mode == "max":
                    target = MAX_KW
                elif st.mode == "price":
                    # Preisgesteuert: <= Median -> MAX, sonst MIN
                    if cur_price is not None and med_price is not None:
                        target = MAX_KW if cur_price <= med_price else MIN_KW
                    else:
                        target = MIN_KW
                    # 100% bis 07:00 sicherstellen
                    cutoff = next_dt("07:00", tzname)
                    now_local = datetime.now(ZoneInfo(tzname))
                    hours_left = max(0.0, (cutoff - now_local).total_seconds() / 3600.0)
                    if hours_left > 0.0:
                        soc_now = float(st.current_soc if st.current_soc is not None else (st.soc or 0))
                        need_soc = max(0.0, 100 - soc_now)
                        need_kwh = (need_soc / 100.0) * battery_kwh
                        eff = max(0.5, min(1.0, efficiency))
                        req_kw = (need_kwh / hours_left) / eff if need_kwh > 0 else 0.0
                        target = max(target, req_kw)
                else:  # eco
                    target = eco_kw
                    if st.boost_enabled:
                        cutoff = next_dt(st.boost_cutoff_local, tzname)
                        now_local = datetime.now(ZoneInfo(tzname))
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
                cp = app.state.cps.get(cp_id)
                if cp:
                    await cp.push_charging_profile(st.target_kw)

            # Wartezeit: 15 Minuten
            await asyncio.sleep(900)

        except Exception as e:
            log.exception("control loop error: %s", e)
            await asyncio.sleep(30)
