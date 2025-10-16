from typing import Dict, Optional, Literal, List, Tuple
from dataclasses import dataclass
from datetime import datetime

Mode = Literal["eco", "max", "off", "price", "manual"]

@dataclass
class ChargePointState:
    id: str
    connected: bool = False
    last_heartbeat: Optional[datetime] = None
    mode: Mode = "eco"

    # Soll-Leistung (kW), wie vom Modus oder Manual vorgegeben
    target_kw: float = 0.0

    # elektrische Parameter
    phase_count: int = 3
    voltage_per_phase: float = 230.0
    max_current_a: float = 16.0

    # CP-Status
    cp_status: Optional[str] = None
    error_code: Optional[str] = None

    # SoC und Ist-Leistung
    soc: Optional[int] = None
    current_soc: Optional[int] = None
    current_kw: Optional[float] = None

    # Eco-Boost
    boost_enabled: bool = False
    boost_cutoff_local: str = "07:00"
    boost_target_soc: int = 100
    boost_reached_notified: bool = False

    # Energie-Register insgesamt (kWh)
    energy_kwh_total: Optional[float] = None

    # Session-Tracking
    tx_active: bool = False
    session_start_at: Optional[datetime] = None
    session_start_kwh_reg: Optional[float] = None   # Zählerstand zu Beginn (kWh)
    session_kwh: Optional[float] = None             # geladene kWh in aktueller Session
    session_est_end_at: Optional[datetime] = None   # prognostiziertes Ende
    session_id: Optional[int] = None

STATE: Dict[str, ChargePointState] = {}
ENERGY_LOGS: Dict[str, List[Tuple[datetime, float]]] = {}
