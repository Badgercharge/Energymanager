import os, logging, aiohttp
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

log = logging.getLogger(__name__)

"""
Konfigurierbarer Preis-Provider:
- PRICE_API_URL muss eine Liste von Preispunkten liefern (15-Minuten-Takt empfohlen).
- Unterstützte Formate je Element (wir parsen flexibel):
  { "start": ISO8601, "price_ct_per_kwh": float }
  { "ts": ISO8601, "ct_per_kwh": float }
  { "start": ISO8601, "price_eur_per_mwh": float }  # wird /10 umgerechnet zu ct/kWh
Wir nehmen alle Punkte der letzten 24h bis +24h und bilden den Median aller Preise des heutigen Kalendertags (lokal).
"""

async def fetch_prices_ct_per_kwh(now: datetime) -> List[Tuple[datetime, float]]:
    url = os.getenv("PRICE_API_URL")
    if not url:
        # Kein Provider konfiguriert -> keine Preise
        return []
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=10) as r:
            j = await r.json()
    out: List[Tuple[datetime, float]] = []
    for item in j:
        ts_str = item.get("start") or item.get("ts")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z","+00:00"))
        except Exception:
            continue
        if "price_ct_per_kwh" in item:
            price = float(item["price_ct_per_kwh"])
        elif "ct_per_kwh" in item:
            price = float(item["ct_per_kwh"])
        elif "price_eur_per_mwh" in item:
            price = float(item["price_eur_per_mwh"]) / 10.0  # 1 EUR/MWh = 0.1 ct/kWh
        else:
            continue
        out.append((ts.astimezone(timezone.utc), price))
    # Sortieren und filtern auf +-36h um now
    out.sort(key=lambda x: x[0])
    lo = now - timedelta(hours=36)
    hi = now + timedelta(hours=36)
    out = [p for p in out if lo <= p[0] <= hi]
    return out

def median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid-1] + s[mid]) / 2.0
