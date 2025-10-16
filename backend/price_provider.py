import os, logging, aiohttp
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

log = logging.getLogger(__name__)

AWATTAR_DE = "https://api.awattar.de/v1/marketdata"

def _to_dt_ms(ms: int) -> datetime:
    # aWATTar liefert ms seit Epoche (UTC)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

async def fetch_prices_ct_per_kwh(now: datetime) -> List[Tuple[datetime, float]]:
    """
    Holt Day-Ahead-Preise von aWATTar (DE), konvertiert zu ct/kWh
    und expandiert Stundenpreise in 15-Minuten-Slots (flach).
    Rückgabe: Liste[(ts_start, ct_per_kwh)] in UTC, auf +-36h um now begrenzt.
    """
    url = os.getenv("PRICE_API_URL", AWATTAR_DE)
    params = {}  # aWATTar ohne Key, optional könnten time_from/time_to gesetzt werden
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=12) as r:
                r.raise_for_status()
                j = await r.json()
    except Exception as e:
        log.exception("price fetch failed: %s", e)
        return []

    data = j.get("data") if isinstance(j, dict) else j
    if not isinstance(data, list):
        log.warning("unexpected price payload shape: %s", type(j))
        return []

    out: List[Tuple[datetime, float]] = []
    for item in data:
        # aWATTar Felder: start_timestamp, end_timestamp (ms), marketprice (EUR/MWh)
        try:
            start = _to_dt_ms(int(item["start_timestamp"]))
            end   = _to_dt_ms(int(item["end_timestamp"]))
            eur_per_mwh = float(item["marketprice"])
            ct_per_kwh = eur_per_mwh / 10.0  # 1 EUR/MWh = 0.1 ct/kWh
        except Exception:
            continue

        # Stundenpreis auf 15-Minuten-Slots verteilen: start, +15, +30, +45
        slot = start
        while slot < end:
            out.append((slot, ct_per_kwh))
            slot = slot + timedelta(minutes=15)

    # sortieren und auf +-36h um now beschränken
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
    m = n // 2
    return s[m] if n % 2 == 1 else (s[m - 1] + s[m]) / 2.0
