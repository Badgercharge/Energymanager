import React, { useEffect, useState } from 'react'

const API = import.meta.env.VITE_API || window.location.origin

// --- API ---
async function fetchPoints() {
  const r = await fetch(`${API}/api/points`)
  if (!r.ok) throw new Error(`API error ${r.status}`)
  return r.json()
}
async function fetchEcoConfig() {
  const r = await fetch(`${API}/api/config/eco`)
  if (!r.ok) throw new Error(`API error ${r.status}`)
  return r.json()
}
async function saveEcoConfig(data) {
  const r = await fetch(`${API}/api/config/eco`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(data)
  })
  if (!r.ok) throw new Error(`API error ${r.status}`)
  return r.json()
}
async function setModeAPI(id, mode) {
  await fetch(`${API}/api/points/${id}/mode/${mode}`, { method: 'POST' })
}
async function setLimitAPI(id, kw) {
  await fetch(`${API}/api/points/${id}/limit?kw=${kw}`, { method: 'POST' })
}
async function setSocAPI(id, soc) {
  await fetch(`${API}/api/points/${id}/soc`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ soc: Number(soc) })
  })
}
async function fetchBoost(id) {
  const r = await fetch(`${API}/api/points/${id}/boost`)
  if (!r.ok) throw new Error(`API error ${r.status}`)
  return r.json()
}
async function saveBoost(id, data) {
  await fetch(`${API}/api/points/${id}/boost`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(data)
  })
}

// --- UI Blocks ---
function ModeInfo() {
  return (
    <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm">
      <div className="px-4 py-3 border-b border-slate-100">
        <h3 className="text-sm font-semibold text-slate-800">Modi & Eco</h3>
      </div>
      <div className="p-4 text-sm leading-relaxed text-slate-600 space-y-2">
        <p><strong>Eco</strong>: PV‑geführt. Ladeleistung wird anhand der Sonneneinstrahlung zwischen <strong>CLOUDY_KW</strong> und <strong>SUNNY_KW</strong> geregelt (automatisch gemappt), gedeckelt durch das interne Base‑Limit.</p>
        <p><strong>Boost (im Eco)</strong>: Bis zur eingestellten Uhrzeit wird – falls nötig – zusätzlich Leistung gegeben, um den Ziel‑SoC zu erreichen. Danach läuft wieder normales Eco.</p>
        <p><strong>Max</strong>: Konstantes Laden mit dem internen Base‑Limit.</p>
        <p><strong>Aus</strong>: Ziel 0 kW (pausiert effektiv das Laden).</p>
      </div>
    </div>
  )
}

function EcoSettings({ cfg, onSave }) {
  const [sunny, setSunny] = useState(cfg?.sunny_kw ?? 11.0)
  const [cloudy, setCloudy] = useState(cfg?.cloudy_kw ?? 3.7)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!cfg) return
    setSunny(cfg.sunny_kw ?? 11.0)
    setCloudy(cfg.cloudy_kw ?? 3.7)
  }, [cfg])

  const save = async () => {
    setSaving(true)
    try {
      await onSave({ sunny_kw: Number(sunny), cloudy_kw: Number(cloudy) })
      alert("Eco gespeichert.")
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm">
      <div className="px-4 py-3 border-b border-slate-100">
        <h3 className="text-sm font-semibold text-slate-800">Eco‑Einstellungen</h3>
      </div>
      <div className="p-4 grid gap-3 md:grid-cols-3">
        <label className="text-sm">SUNNY_KW
          <input type="number" step="0.1" min="0" value={sunny}
                 onChange={e=>setSunny(e.target.value)}
                 className="mt-1 block border rounded px-2 py-1 w-full"/>
        </label>
        <label className="text-sm">CLOUDY_KW
          <input type="number" step="0.1" min="0" value={cloudy}
                 onChange={e=>setCloudy(e.target.value)}
                 className="mt-1 block border rounded px-2 py-1 w-full"/>
        </label>
        <div className="flex items-end">
          <button disabled={saving} onClick={save}
                  className="px-3 py-2 bg-emerald-600 text-white rounded w-full">
            {saving ? "Speichern…" : "Speichern"}
          </button>
        </div>
      </div>
    </div>
  )
}

function BoostPanel({ point, onSaved }) {
  const [enabled, setEnabled] = useState(false)
  const [cutoff, setCutoff] = useState("07:00")
  const [targetSoc, setTargetSoc] = useState(80)
  const [soc, setSoc] = useState(point.current_soc ?? point.soc ?? 40)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const b = await fetchBoost(point.id)
        if (!alive) return
        setEnabled(!!b.enabled)
        setCutoff(b.cutoff_local || "07:00")
        setTargetSoc(b.target_soc ?? 80)
      } finally {
        setLoading(false)
      }
    })()
    return () => { alive = false }
  }, [point.id])

  useEffect(() => {
    setSoc(point.current_soc ?? point.soc ?? soc)
  }, [point.current_soc, point.soc])

  const save = async () => {
    setSaving(true)
    try {
      await saveBoost(point.id, {
        enabled,
        cutoff_local: cutoff,
        target_soc: Number(targetSoc)
      })
      onSaved && onSaved()
      alert("Boost gespeichert.")
    } finally {
      setSaving(false)
    }
  }

  const saveSocNow = async () => {
    setSaving(true)
    try {
      await setSocAPI(point.id, Number(soc))
      onSaved && onSaved()
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mt-4 border-t pt-3">
      <div className="text-sm font-semibold mb-2">Boost (im Eco)</div>
      {loading ? <div className="text-sm text-slate-500">Lade…</div> : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)} />
              <span>Aktiv</span>
            </label>
            <label className="text-sm">Bis Uhrzeit
              <input type="time" value={cutoff} onChange={e=>setCutoff(e.target.value)}
                     className="block border rounded px-2 py-1 w-full mt-1" />
            </label>
            <label className="text-sm">Ziel‑SoC (%)
              <input type="number" value={targetSoc} min={10} max={100}
                     onChange={e=>setTargetSoc(e.target.value)}
                     className="block border rounded px-2 py-1 w-full mt-1" />
            </label>
            <div className="md:col-span-3 flex justify-end">
              <button disabled={saving} onClick={save}
                      className="px-3 py-2 bg-teal-700 text-white rounded">
                {saving ? "Speichern…" : "Speichern"}
              </button>
            </div>
          </div>

          <div className="mt-3 grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3 items-end">
            <label className="text-sm">Aktueller SoC (%)
              <input type="number" value={soc} min={0} max={100}
                     onChange={e=>setSoc(e.target.value)}
                     className="block border rounded px-2 py-1 w-full mt-1"/>
            </label>
            <button disabled={saving} onClick={saveSocNow}
                    className="px-3 py-2 bg-indigo-600 text-white rounded">
              SoC aktualisieren
            </button>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            Boost hebt Eco nur an, wenn nötig. Nach {cutoff} läuft wieder normales Eco weiter.
          </p>
        </>
      )}
    </div>
  )
}

// --- App ---
export default function App() {
  const [points, setPoints] = useState([])
  const [eco, setEco] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const load = async () => {
    try {
      setLoading(true); setError(null)
      const [pts, cfg] = await Promise.all([fetchPoints(), fetchEcoConfig()])
      setPoints(Array.isArray(pts) ? pts : [])
      setEco(cfg)
    } catch (e) { setError(e.message || String(e)) }
    finally { setLoading(false) }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  const setMode = async (id, mode) => { await setModeAPI(id, mode); load() }
  const setLimit = async (id) => {
    const kw = parseFloat(prompt("Ziel kW:"))
    if (!isNaN(kw)) { await setLimitAPI(id, kw); load() }
  }
  const saveEco = async (data) => { await saveEcoConfig(data); await load() }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/70 backdrop-blur-sm">
        <div className="max-w-5xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-2.5 h-2.5 rounded-full bg-emerald-500" />
            <h1 className="text-base font-semibold tracking-tight">Badger‑charge</h1>
          </div>
          <div className="text-xs text-slate-500">Backend: {API}</div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-6 space-y-6">
        <ModeInfo />
        <EcoSettings cfg={eco} onSave={saveEco} />

        {loading && <div className="text-sm text-slate-500">Lade…</div>}
        {error && <div className="text-sm text-red-600">Fehler: {error}</div>}

        <div className="grid gap-4">
          {points.map(p => (
            <div key={p.id} className="rounded-xl border border-slate-200 bg-white/80 shadow-sm p-4">
              <div className="flex justify-between gap-4">
                <div>
                  <div className="font-semibold">{p.id}</div>
                  <div className="text-sm">
                    {p.connected ? "verbunden" : "getrennt"} · Modus: {p.mode}
                  </div>
                  {p.current_soc != null && (
                    <div className="text-xs text-slate-500">SoC (auto): {p.current_soc}%</div>
                  )}
                  {p.last_heartbeat && (
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
                <button onClick={() => setLimit(p.id)} className="px-3 py-1 bg-amber-600 text-white rounded">kW setzen</button>
              </div>

              <BoostPanel point={p} onSaved={load} />
            </div>
          ))}

          {points.length === 0 && !loading && (
            <div className="text-slate-500">
              Noch keine Wallbox verbunden… (Simulator oder Wallbox mit wss://…/ocpp/&lt;CP_ID&gt; verbinden)
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
