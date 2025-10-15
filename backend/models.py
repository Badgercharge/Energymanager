from typing import Dict, Optional, Literal, List, Tuple
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

    # SoC
    soc: Optional[int] = None
    current_soc: Optional[int] = None

    # Boost im Eco
    boost_enabled: bool = False
    boost_cutoff_local: str = "07:00"  # HH:MM
    boost_target_soc: int = 80         # %
    boost_reached_notified: bool = False  # Mail nur einmal

    # Energie / Statistik
    energy_kwh_total: Optional[float] = None   # letzter bekannter Gesamtzähler (kWh)
    # Historie einfacher Zählerstände: (timestamp, kwh_total)
    # Nicht persistiert, geht bei Backend-Reboot verloren (kann später in DB)
    # Für die API wird daraus die Wochen/Monatsmenge berechnet.
    # Diese Liste liegt global in ENERGY_LOGS (s.u.)

STATE: Dict[str, ChargePointState] = {}

# Energie-Logs: cp_id -> Liste[(datetime, kwh_total)]
ENERGY_LOGS: Dict[str, List[Tuple[datetime, float]]] = {}
