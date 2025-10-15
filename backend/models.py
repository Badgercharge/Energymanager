# backend/models.py
from typing import Dict, Optional, Literal
from dataclasses import dataclass
from datetime import datetime

Mode = Literal["eco","max","off","schedule"]

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
    soc: Optional[int] = None

    # Schedule-Parameter
    schedule_enabled: bool = False
    cutoff_local: str = "07:00"      # HH:MM lokale Zeit
    target_soc: int = 80             # %
    current_soc: int = 40            # %
    battery_kwh: float = 60.0        # kWh
    charge_efficiency: float = 0.92  # 92% Standardwirkungsgrad

STATE: Dict[str, ChargePointState] = {}
