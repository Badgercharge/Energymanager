from typing import Dict, Optional, Literal, List, Tuple
from dataclasses import dataclass
from datetime import datetime

Mode = Literal["eco", "max", "off", "price", "manual"]  # manual ergänzt

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

    cp_status: Optional[str] = None
    error_code: Optional[str] = None

    soc: Optional[int] = None
    current_soc: Optional[int] = None
    current_kw: Optional[float] = None

    boost_enabled: bool = False
    boost_cutoff_local: str = "07:00"
    boost_target_soc: int = 100
    boost_reached_notified: bool = False

    energy_kwh_total: Optional[float] = None

STATE: Dict[str, ChargePointState] = {}
ENERGY_LOGS: Dict[str, List[Tuple[datetime, float]]] = {}
