from typing import Dict, Optional, Literal
from dataclasses import dataclass
from datetime import datetime

Mode = Literal["eco", "max", "off"]

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

    # SoC (auto via MeterValues oder manuell)
    soc: Optional[int] = None
    current_soc: Optional[int] = None

    # Boost im Eco (per Lader)
    boost_enabled: bool = False
    boost_cutoff_local: str = "07:00"  # HH:MM lokale Zeit
    boost_target_soc: int = 80         # %
    
STATE: Dict[str, ChargePointState] = {}
