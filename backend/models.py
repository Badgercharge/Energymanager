from typing import Dict, Optional, Literal, List, Tuple
from dataclasses import dataclass
from datetime import datetime

# Unterstützte Modi inkl. "manual"
Mode = Literal["eco", "max", "off", "price", "manual"]

@dataclass
class ChargePointState:
    id: str
    connected: bool = False
    last_heartbeat: Optional[datetime] = None

    # Aktueller Modus und Soll-Leistung (kW) – wird vom Scheduler bzw. bei "manual" gesetzt
    mode: Mode = "eco"
    target_kw: float = 0.0

    # Elektrische Parameter (für Umrechnung kW <-> A in SetChargingProfile)
    phase_count: int = 3
    voltage_per_phase: float = 230.0
    max_current_a: float = 16.0

    # Status laut StatusNotification
    cp_status: Optional[str] = None     # z. B. Available / Charging / Faulted
    error_code: Optional[str] = None

    # SoC (nur read-only via OCPP) und Ist-Leistung
    soc: Optional[int] = None
    current_soc: Optional[int] = None
    current_kw: Optional[float] = None

    # Eco-Boost (Standard bei Session-Start: 100% bis 07:00)
    boost_enabled: bool = False
    boost_cutoff_local: str = "07:00"
    boost_target_soc: int = 100
    boost_reached_notified: bool = False

    # Energie-Register gesamt (kWh)
    energy_kwh_total: Optional[float] = None

    # Session-Tracking
    tx_active: bool = False
    session_id: Optional[int] = None
    session_start_at: Optional[datetime] = None
    session_start_kwh_reg: Optional[float] = None   # Registerstand zu Beginn (kWh)
    session_kwh: Optional[float] = None             # bisher geladene kWh in aktueller Session
    session_est_end_at: Optional[datetime] = None   # prognostiziertes Ende (lokale Zeit)

# Globaler In-Memory-Status
STATE: Dict[str, ChargePointState] = {}

# Energie-Logs für Statistik: cp_id -> Liste[(timestamp_utc, kwh_total)]
ENERGY_LOGS: Dict[str, List[Tuple[datetime, float]]] = {}
