import React, { useEffect, useState } from 'react'

const API = import.meta.env.VITE_API || window.location.origin

async function saveSchedule(id, data) {
  await fetch(`${API}/api/points/${id}/schedule`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(data)
  })
}

async function setSocAPI(id, soc) {
  await fetch(`${API}/api/points/${id}/soc`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ soc: Number(soc) })
  })
}

function ScheduleForm({ p, onSaved }) {
  const [enabled, setEnabled] = React.useState(p.schedule_enabled ?? false)
  const [cutoff, setCutoff] = React.useState(p.cutoff_local || "07:00")
  const [targetSoc, setTargetSoc] = React.useState(p.target_soc ?? 80)
  const [batteryKwh, setBatteryKwh] = React.useState(p.battery_kwh ?? 60)
  const [eff, setEff] = React.useState(p.charge_efficiency ?? 0.92)
  const [soc, setSOC] = React.useState(p.current_soc ?? p.soc ?? 40)

  const save = async () => {
    await saveSchedule(p.id, {
      enabled,
      cutoff_local: cutoff,
      target_soc: Number(targetSoc),
      battery_kwh: Number(batteryKwh),
      charge_efficiency: Number(eff)
    })
    onSaved && onSaved()
    alert("Zeitplan gespeichert.")
  }

  const saveSoc = async () => {
    await setSocAPI(p.id, Number(soc))
    onSaved && onSaved()
  }

  return (
    <div className="mt-4 border-t pt-3">
      <div className="text-sm font-semibold mb-2">Zeitplan (SoC bis Uhrzeit)</div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)} />
          <span>Aktiv</span>
        </label>
        <label className="text-sm">
          Ausschalten um
          <input type="time" value={cutoff} onChange={e=>setCutoff(e.target.value)}
                 className="block border rounded px-2 py-1 w-full mt-1"/>
        </label>
        <label className="text-sm">
          Ziel‑SoC (%)
          <input type="number" value={targetSoc} min={10} max={100}
                 onChange={e=>setTargetSoc(e.target.value)}
                 className="block border rounded px-2 py-1 w-full mt-1"/>
        </label>
        <label className="text-sm">
          Batterie (kWh)
          <input type="number" value={batteryKwh} min={10} max={120} step="0.5"
                 onChange={e=>setBatteryKwh(e.target.value)}
                 className="block border rounded px-2 py-1 w-full mt-1"/>
        </label>
        <label className="text-sm">
          Wirkungsgrad
          <input type="number" value={eff} min={0.5} max={1.0} step="0.01"
                 onChange={e=>setEff(e.target.value)}
                 className="block border rounded px-2 py-1 w-full mt-1"/>
        </label>
        <div className="flex items-end">
          <button onClick={save} className="px-3 py-2 bg-emerald-600 text-white rounded w-full">Zeitplan speichern</button>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
        <label className="text-sm">
          Aktueller SoC (%)
          <input type="number" value={soc} min={0} max={100}
                 onChange={e=>setSOC(e.target.value)}
                 className="block border rounded px-2 py-1 w-full mt-1"/>
        </label>
        <div className="flex items-end">
          <button onClick={saveSoc} className="px-3 py-2 bg-indigo-600 text-white rounded w-full">SoC aktualisieren</button>
        </div>
        <div className="text-xs text-slate-500">
          Logik: Eco (Wetter) + Zusatzleistung, um Ziel‑SoC bis {cutoff} zu erreichen. Nach {cutoff} wird abgeschaltet.
        </div>
      </div>
    </div>
  )
}

export default function App() {
  const [points, setPoints] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const load = async () => {
    try {
      setLoading(true)
      setError(null)
      const r = await fetch(`${API}/api/points`)
      if (!r.ok) throw new Error(`API error ${r.status}`)
      const data = await r.json()
      setPoints(Array.isArray(data) ? data : [])
    } catch (e) {
      console.error(e)
      setError(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }

  const setMode = async (id, mode) => {
    await fetch(`${API}/api/points/${id}/mode/${mode}`, { method: 'POST' })
    load()
  }

  const setLimit = async (id) => {
    const kw = parseFloat(prompt("Ziel kW:"))
    if (!isNaN(kw)) {
      await fetch(`${API}/api/points/${id}/limit?kw=${kw}`, { method: 'POST' })
      load()
    }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <div className="max-w-4xl mx-auto py-8 px-4">
        <h1 className="text-2xl font-bold mb-1">Heim‑EMS (PV‑geführt)</h1>
        <p className="text-sm text-slate-500 mb-6">Backend: {API}</p>

        {loading && <div className="mb-4 text-sm text-slate-500">Lade…</div>}
        {error && <div className="mb-4 text-sm text-red-600">Fehler: {error}</div>}

        <div className="grid gap-4">
          {points.map(p => (
            <div key={p.id} className="rounded-md bg-white shadow p-4">
              <div className="flex justify-between gap-4">
                <div>
                  <div className="font-semibold">{p.id}</div>
                  <div className="text-sm">
                    {p.connected ? "verbunden" : "getrennt"} · Modus: {p.mode}
                  </div>
                  {(p.last_heartbeat) && (
                    <div className="text-xs text-slate-500">Letzter Heartbeat: {String(p.last_heartbeat)}</div>
                  )}
                </div>
                <div className="text-right">
                  <div className="text-2xl font-bold">{Number(p.target_kw || 0).toFixed(2)} kW</div>
                  <div className="text-xs text-slate-500">Ziel‑Ladeleistung</div>
                </div>
              </div>

              <div className="mt-3 flex flex-wrap gap-2">
                <button onClick={() => setMode(p.id, "eco")} className="px-3 py-1 bg-emerald-600 text-white rounded">Eco</button>
                <button onClick={() => setMode(p.id, "max")} className="px-3 py-1 bg-indigo-600 text-white rounded">Max</button>
                <button onClick={() => setMode(p.id, "off")} className="px-3 py-1 bg-slate-600 text-white rounded">Aus</button>
                <button onClick={() => setMode(p.id, "schedule")} className="px-3 py-1 bg-teal-700 text-white rounded">Schedule</button>
                <button onClick={() => setLimit(p.id)} className="px-3 py-1 bg-amber-600 text-white rounded">kW setzen</button>
              </div>

              {/* Zeitplan-Panel */}
              <ScheduleForm p={p} onSaved={load} />
            </div>
          ))}

          {points.length === 0 && !loading && (
            <div className="text-slate-500">
              Noch keine Wallbox verbunden… (Simulator oder Wallbox mit wss://…/ocpp/&lt;CP_ID&gt; verbinden)
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
