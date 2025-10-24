# backend/ocpp_cs.py (Kopfbereich)
import os
import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone

from ocpp.v16 import ChargePoint as CP, call, call_result
from ocpp.v16.enums import Action, RegistrationStatus, ChargePointStatus, ChargingRateUnitType
from ocpp.routing import on

log = logging.getLogger("ocpp")

# CP-ID fest verdrahtet, aber per ENV überschreibbar
DEFAULT_CP_ID = os.getenv("DEFAULT_CP_ID", "504000093")

# Whitelist: per ENV KNOWN_CP_IDS=504000093,cp2,cp3 ...
KNOWN_CP_IDS = {s.strip() for s in os.getenv("KNOWN_CP_IDS", DEFAULT_CP_ID).split(",") if s.strip()}

def extract_cp_id_from_path(path: str) -> str:
    """
    Erwartete Pfade:
      /ocpp/504000093
      /ocpp
    Fallback: DEFAULT_CP_ID
    """
    parts = [p for p in path.split("/") if p]
    # ["ocpp", "504000093"] -> ID an Pos 1
    if len(parts) >= 2 and parts[0].lower() == "ocpp":
        return parts[1]
    # Nur "/ocpp" -> fallback
    if len(parts) == 1 and parts[0].lower() == "ocpp":
        return DEFAULT_CP_ID
    # Irgendwas anderes -> fallback
    return DEFAULT_CP_ID
