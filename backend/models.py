from typing import Dict, Optional, Literal, List, Tuple
from dataclasses import dataclass
from datetime import datetime

Mode = Literal["eco", "max", "off", "price"]

@dataclass
class ChargePointState:
    id: str
    connected: bool = False
    last_heartbeat: Optional[datetime] = None
    mode: Mode = "eco"
    target_kw: float = 0.0
    phase_count: int = 3
    voltage_per_phase: float = 230.0
    max_current_a: float = 16.0

    # Betriebsstatus aus StatusNotification (Available/Charging/Faulted...)
    cp_status: Optional[str] = None
    error_code: Optional[str] = None

    # SoC
    soc: Optional[int] = None
    current_soc: Optional[int] = None

    # Eco-Boost (Standard bei Session-Start: 100% bis 07:00)
    boost_enabled: bool = False
    boost_cutoff_local: str = "07:00"  # HH:MM
    boost_target_soc: int = 100
    boost_reached_notified: bool = False

    # Energie / Statistik (kumul. Register kWh)
    energy_kwh_total: Optional[float] = None

STATE: Dict[str, ChargePointState] = {}

# Energie-Logs: cp_id -> Liste[(timestamp, kwh_total)]
ENERGY_LOGS: Dict[str, List[Tuple[datetime, float]]] = {}
