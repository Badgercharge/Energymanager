import React, { useEffect, useState } from 'react'

const API = import.meta.env.VITE_API || window.location.origin

export default function App() {
  const [points, setPoints] = useState([])
  const load = async () => {
    try {
      const r = await fetch(`${API}/api/points`)
      setPoints(await r.json())
    } catch (e) {
      console.error(e)
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
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [])
  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <div className="max-w-4xl mx-auto py-8 px-4">
        <h1 className="text-2xl font-bold mb-4">Heim‑EMS (PV‑geführt)</h1>
        <p className="text-sm text-slate-500 mb-4">
          Backend: {API}
        </p>
        <div className="grid gap-4">
          {points.map(p => (
            <div key={p.id} className="rounded-md bg-white shadow p-4">
              <div className="flex justify-between">
                <div>
                  <div className="font-semibold">{p.id}</div>
                  <div className="text-sm">{p.connected ? "verbunden" : "getrennt"} · Modus: {p.mode}</div>
                </div>
                <div className="text-right">
                  <div className="text-2xl font-bold">{Number(p.target_kw || 0).toFixed(2)} kW</div>
                  <div className="text-xs text-slate-500">Ziel‑Ladeleistung</div>
                </div>
              </div>
              <div className="mt-3 flex gap-2">
                <button onClick={() => setMode(p.id, "eco")} className="px-3 py-1 bg-emerald-600 text-white rounded">Eco</button>
                <button onClick={() => setMode(p.id, "max")} className="px-3 py-1 bg-indigo-600 text-white rounded">Max</button>
                <button onClick={() => setMode(p.id, "off")} className="px-3 py-1 bg-slate-600 text-white rounded">Aus</button>
                <button onClick={() => setLimit(p.id)} className="px-3 py-1 bg-amber-600 text-white rounded">kW setzen</button>
              </div>
            </div>
          ))}
          {points.length === 0 && <div className="text-slate-500">Noch keine Wallbox verbunden…</div>}
        </div>
      </div>
    </div>
  )
}
