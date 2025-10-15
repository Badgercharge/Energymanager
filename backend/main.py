# backend/main.py – nur neue/änderte Teile zeigen

from fastapi import FastAPI, WebSocket, Body
# ...

@app.get("/api/points/{cp_id}/schedule")
def get_schedule(cp_id: str):
    s = STATE.get(cp_id)
    if not s:
        return {"enabled": False}
    return {
        "enabled": s.schedule_enabled,
        "cutoff_local": s.cutoff_local,
        "target_soc": s.target_soc,
        "current_soc": s.current_soc,
        "battery_kwh": s.battery_kwh,
        "charge_efficiency": s.charge_efficiency,
    }

@app.post("/api/points/{cp_id}/schedule")
def set_schedule(
    cp_id: str,
    enabled: bool = Body(...),
    cutoff_local: str = Body(..., embed=True),   # "HH:MM"
    target_soc: int = Body(...),
    battery_kwh: float = Body(...),
    charge_efficiency: float = Body(0.92),
):
    from models import ChargePointState
    s = STATE.get(cp_id) or ChargePointState(id=cp_id)
    s.schedule_enabled = enabled
    s.cutoff_local = cutoff_local
    s.target_soc = int(target_soc)
    s.battery_kwh = float(battery_kwh)
    s.charge_efficiency = float(charge_efficiency)
    STATE[cp_id] = s
    # Modus automatisch auf "schedule" setzen, wenn enabled
    s.mode = "schedule" if enabled else s.mode
    return {"ok": True}

@app.post("/api/points/{cp_id}/soc")
def set_soc(cp_id: str, soc: int = Body(..., embed=True)):
    if cp_id not in STATE:
        from models import ChargePointState
        STATE[cp_id] = ChargePointState(id=cp_id)
    STATE[cp_id].current_soc = int(soc)
    STATE[cp_id].soc = int(soc)  # optional Spiegelfeld
    return {"ok": True}
