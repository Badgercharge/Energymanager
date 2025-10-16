import React, { useEffect, useState } from 'react'

const API = import.meta.env.VITE_API || window.location.origin
const MIN_KW = 3.7
const MAX_KW = 11.0

async function fetchPoints(){ const r=await fetch(`${API}/api/points`); if(!r.ok) throw new Error(`API ${r.status}`); return r.json() }
async function fetchEco(){ const r=await fetch(`${API}/api/config/eco`); if(!r.ok) throw new Error(`API ${r.status}`); return r.json() }
async function saveEco(data){ const r=await fetch(`${API}/api/config/eco`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); if(!r.ok) throw new Error(`API ${r.status}`); return r.json() }
async function setMode(id,mode){ await fetch(`${API}/api/points/${id}/mode/${mode}`,{method:'POST'}) }
async function setLimit(id,kw){ await fetch(`${API}/api/points/${id}/limit?kw=${kw}`,{method:'POST'}) }
async function fetchStats(){ const r=await fetch(`${API}/api/stats`); if(!r.ok) throw new Error(`API ${r.status}`); return r.json() }
async function fetchBoost(id){ const r=await fetch(`${API}/api/points/${id}/boost`); if(!r.ok) throw new Error(`API ${r.status}`); return r.json() }
async function saveBoost(id,data){ await fetch(`${API}/api/points/${id}/boost`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}) }
async function fetchPrice(){ const r=await fetch(`${API}/api/price`); if(!r.ok) throw new Error(`API ${r.status}`); return r.json() }
async function fetchWeather(){ const r=await fetch(`${API}/api/weather`); if(!r.ok) throw new Error(`API ${r.status}`); return r.json() }

function ModeInfo(){
  return (
    <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm">
      <div className="px-4 py-3 border-b border-slate-100"><h3 className="text-sm font-semibold text-slate-800">Modi</h3></div>
      <div className="p-4 text-sm text-slate-600 space-y-2">
        <p><strong>Eco</strong>: PV‑geführt (zwischen CLOUDY_KW und SUNNY_KW) mit optionalem Boost bis Uhrzeit.</p>
        <p><strong>Price</strong>: Preisgeführt im 15‑Min‑Raster. ≤ Median: {MAX_KW} kW, sonst {MIN_KW} kW. 100% bis 07:00 abgesichert.</p>
        <p><strong>Max</strong>: Konstant {MAX_KW} kW. · <strong>Aus</strong>: 0 kW </p>
        <p><strong>Manuell</strong>: Wird aktiv, wenn du „kW setzen“ verwendest; bleibt aktiv bis du wieder einen anderen Modus wählst.</p>
      </div>
    </div>
  )
}

function EcoSettings({cfg,onSave}){
  const [sunny,setSunny]=useState(cfg?.sunny_kw??MAX_KW)
  const [cloudy,setCloudy]=useState(cfg?.cloudy_kw??MIN_KW)
  const [saving,setSaving]=useState(false)
  useEffect(()=>{ if(cfg){ setSunny(cfg.sunny_kw); setCloudy(cfg.cloudy_kw) } },[cfg])
  const clamp=(v)=>Math.max(MIN_KW,Math.min(MAX_KW,Number(v)))
  const save=async()=>{ setSaving(true); try{ await onSave({sunny_kw:clamp(sunny),cloudy_kw:clamp(cloudy)}); alert("Eco gespeichert.") } finally{ setSaving(false) } }
  return (
    <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm">
      <div className="px-4 py-3 border-b border-slate-100"><h3 className="text-sm font-semibold text-slate-800">Eco‑Einstellungen</h3></div>
      <div className="p-4 grid gap-3 md:grid-cols-3">
        <label className="text-sm">SUNNY_KW
          <input type="number" step="0.1" min={MIN_KW} max={MAX_KW} value={sunny} onChange={e=>setSunny(e.target.value)} className="mt-1 block border rounded px-2 py-1 w-full"/>
        </label>
        <label className="text-sm">CLOUDY_KW
          <input type="number" step="0.1" min={MIN_KW} max={MAX_KW} value={cloudy} onChange={e=>setCloudy(e.target.value)} className="mt-1 block border rounded px-2 py-1 w-full"/>
        </label>
        <div className="flex items-end">
          <button disabled={saving} onClick={save} className="px-3 py-2 bg-emerald-600 text-white rounded w-full">{saving?"Speichern…":"Speichern"}</button>
        </div>
      </div>
    </div>
  )
}

function PricePanel(){
  const [price,setPrice]=useState(null)
  const [err,setErr]=useState(null)
  const load=async()=>{ try{ setErr(null); setPrice(await fetchPrice()) } catch(e){ setErr(e.message||String(e)) } }
  useEffect(()=>{ load(); const t=setInterval(load,60_000); return()=>clearInterval(t) },[])
  if(err) return <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm p-4 text-sm text-red-600">Preisfehler: {err}</div>
  const p = price || {}
  const cur = p.current_ct_per_kwh
  const med = p.median_ct_per_kwh
  const below = p.below_or_equal_median
  const badge = below===true ? {txt:"≤ Median", cls:"bg-emerald-100 text-emerald-700"} :
                below===false ? {txt:"> Median", cls:"bg-red-100 text-red-700"} :
                {txt:"n/a", cls:"bg-slate-100 text-slate-700"}
  return (
    <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm">
      <div className="px-4 py-3 border-b border-slate-100"><h3 className="text-sm font-semibold text-slate-800">Aktueller Strompreis</h3></div>
      <div className="p-4 flex items-center justify-between text-sm">
        <div>
          <div className="text-xs text-slate-500">as of</div>
          <div className="text-slate-700">{p.as_of ? new Date(p.as_of).toLocaleString() : "—"}</div>
        </div>
        <div className="text-right">
          <div className="text-xs text-slate-500">Preis / Median</div>
          <div className="text-lg font-semibold">
            {cur!=null ? cur.toFixed(2) : "—"} / {med!=null ? med.toFixed(2) : "—"} ct/kWh
          </div>
          <div className={`mt-1 inline-block px-2 py-0.5 rounded-full text-xs ${badge.cls}`}>{badge.txt}</div>
        </div>
      </div>
    </div>
  )
}

function WeatherPanel(){
  const [w,setW]=useState(null)
  const [err,setErr]=useState(null)
  const load=async()=>{ try{ setErr(null); setW(await fetchWeather()) } catch(e){ setErr(e.message||String(e)) } }
  useEffect(()=>{ load(); const t=setInterval(load,60_000); return()=>clearInterval(t) },[])
  if(err) return <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm p-4 text-sm text-red-600">Wetterfehler: {err}</div>
  const d = w || {}
  const t = d.temperature_c
  const cc = d.cloud_cover_pct
  const rad = d.shortwave_radiation_wm2
  const wind = d.wind_speed_ms
  const precip = d.precip_mm
  const icon = precip>0.1 ? "🌧️" : (cc>=70 ? "☁️" : (cc>=30 ? "⛅" : "☀️"))
  return (
    <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm">
      <div className="px-4 py-3 border-b border-slate-100"><h3 className="text-sm font-semibold text-slate-800">Wetter</h3></div>
      <div className="p-4 text-sm flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xl">{icon}</span>
          <div>
            <div className="text-slate-700">{t!=null ? `${t.toFixed(1)} °C` : "—"}</div>
            <div className="text-xs text-slate-500">{d.as_of ? new Date(d.as_of).toLocaleTimeString() : ""}</div>
          </div>
        </div>
        <div className="text-right text-xs text-slate-600">
          <div>Bewölkung: {cc!=null ? `${cc}%` : "—"}</div>
          <div>Strahlung: {rad!=null ? `${rad} W/m²` : "—"}</div>
          <div>Wind: {wind!=null ? `${wind.toFixed?.(1) ?? wind} m/s` : "—"}</div>
        </div>
      </div>
    </div>
  )
}

function StatusBadge({status,error}){
  const s=(status||"").toLowerCase()
  let label="Unbekannt", cls="bg-slate-100 text-slate-700"
  if(s==="available"){ label="Verfügbar"; cls="bg-emerald-100 text-emerald-700" }
  else if(s==="charging"){ label="Fahrzeug wird geladen"; cls="bg-indigo-100 text-indigo-700" }
  else if(s==="faulted"){ label="Fehler"; cls="bg-red-100 text-red-700" }
  else if(s==="preparing"||s==="occupied"){ label="Angesteckt"; cls="bg-amber-100 text-amber-700" }
  return <span className={`px-2 py-0.5 rounded-full text-xs ${cls}`}>{label}{error&&s==="faulted"?" · "+error:""}</span>
}

function BoostPanel({point,onSaved}){
  if(point.mode==="price" || point.mode==="manual"){ 
    return <div className="mt-3 text-xs text-slate-500">Boost wird im aktuellen Modus nicht genutzt.</div>
  }
  const [enabled,setEnabled]=useState(false)
  const [cutoff,setCutoff]=useState("07:00")
  const [target,setTarget]=useState(100)
  const [loading,setLoading]=useState(true)
  const [saving,setSaving]=useState(false)
  useEffect(()=>{ let alive=true;(async()=>{ try{ const b=await fetchBoost(point.id); if(!alive)return; setEnabled(!!b.enabled); setCutoff(b.cutoff_local||"07:00"); setTarget(b.target_soc??100) } finally{ setLoading(false) } })(); return()=>{alive=false} },[point.id])
  const save=async()=>{ setSaving(true); try{ await saveBoost(point.id,{enabled,cutoff_local:cutoff,target_soc:Number(target)}); onSaved&&onSaved(); alert("Boost gespeichert.") } finally{ setSaving(false) } }
  return (
    <div className="mt-4 border-t pt-3">
      <div className="text-sm font-semibold mb-2">Boost (Eco)</div>
      {loading? <div className="text-sm text-slate-500">Lade…</div> :
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <label className="flex items-center gap-2"><input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)}/><span>Aktiv</span></label>
        <label className="text-sm">Bis Uhrzeit
          <input type="time" value={cutoff} onChange={e=>setCutoff(e.target.value)} className="block border rounded px-2 py-1 w-full mt-1"/>
        </label>
        <label className="text-sm">Ziel‑SoC (%)
          <input type="number" value={target} min={10} max={100} onChange={e=>setTarget(e.target.value)} className="block border rounded px-2 py-1 w-full mt-1"/>
        </label>
        <div className="md:col-span-3 flex justify-end">
          <button disabled={saving} onClick={save} className="px-3 py-2 bg-teal-700 text-white rounded">{saving?"Speichern…":"Speichern"}</button>
        </div>
      </div>}
    </div>
  )
}

function StatsPanel(){
  const [stats,setStats]=useState(null); const [err,setErr]=useState(null)
  const load=async()=>{ try{ setErr(null); setStats(await fetchStats()) } catch(e){ setErr(e.message||String(e)) } }
  useEffect(()=>{ load(); const t=setInterval(load,30000); return()=>clearInterval(t) },[])
  if(err) return <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm p-4 text-sm text-red-600">Fehler: {err}</div>
  if(!stats) return <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm p-4 text-sm text-slate-500">Lade Statistik…</div>
  const total=stats.total||{}
  return (
    <div className="rounded-xl border border-slate-200 bg-white/80 shadow-sm">
      <div className="px-4 py-3 border-b border-slate-100"><h3 className="text-sm font-semibold text-slate-800">Geladene Energie</h3></div>
      <div className="p-4 text-sm text-slate-700 space-y-3">
        <div className="flex gap-6">
          <div><div className="text-xs text-slate-500">Letzte 7 Tage</div><div className="text-xl font-semibold">{Number(total["7d"]||0).toFixed(2)} kWh</div></div>
          <div><div className="text-xs text-slate-500">Letzte 30 Tage</div><div className="text-xl font-semibold">{Number(total["30d"]||0).toFixed(2)} kWh</div></div>
        </div>
      </div>
    </div>
  )
}

export default function App(){
  const [points,setPoints]=useState([])
  const [eco,setEco]=useState(null)
  const [loading,setLoading]=useState(false)
  const [error,setError]=useState(null)

  const load=async()=>{
    try{
      setLoading(true); setError(null)
      const [pts,cfg]=await Promise.all([fetchPoints(),fetchEco()])
      setPoints(Array.isArray(pts)?pts:[])
      setEco(cfg)
    }catch(e){ setError(e.message||String(e)) }
    finally{ setLoading(false) }
  }

  useEffect(()=>{ load(); const t=setInterval(load,5000); return()=>clearInterval(t) },[])

  const doSetMode=async(id,mode)=>{ await setMode(id,mode); load() }
  const doSetLimit=async(id)=>{
    const kw=parseFloat(prompt(`Manuelles Ziel kW (${MIN_KW}…${MAX_KW}):`))
    if(!isNaN(kw)){
      const v=Math.max(MIN_KW,Math.min(MAX_KW,kw))
      await setLimit(id,v)  // setzt Modus automatisch auf "manual"
      load()
    }
  }
  const saveEcoCfg=async(d)=>{ await saveEco(d); await load() }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/70 backdrop-blur-sm">
        <div className="max-w-5xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2"><div className="w-2.5 h-2.5 rounded-full bg-emerald-500"/><h1 className="text-base font-semibold tracking-tight">Badger‑charge</h1></div>
          <div className="text-xs text-slate-500">Backend: {API}</div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-6 space-y-6">
        <ModeInfo />
        <div className="grid md:grid-cols-2 gap-4">
          <PricePanel />
          <WeatherPanel />
        </div>
        <EcoSettings cfg={eco} onSave={saveEcoCfg}/>
        <StatsPanel />

        {loading && <div className="text-sm text-slate-500">Lade…</div>}
        {error && <div className="text-sm text-red-600">Fehler: {error}</div>}

        <div className="grid gap-4">
          {points.map(p=>(
            <div key={p.id} className="rounded-xl border border-slate-200 bg-white/80 shadow-sm p-4">
              <div className="flex justify-between gap-4">
                <div>
                  <div className="font-semibold">{p.id}</div>
                  <div className="flex items-center gap-2 text-sm">
                    <StatusBadge status={p.cp_status} error={p.error_code}/>
                    <span>· Modus: {p.mode}</span>
                  </div>
                  {p.current_soc!=null && <div className="text-xs text-slate-500">SoC: {p.current_soc}%</div>}
                  {p.last_heartbeat && <div className="text-xs text-slate-500">Letzter Heartbeat: {String(p.last_heartbeat)}</div>}
                </div>
                <div className="text-right">
                  <div className="text-2xl font-bold">{Number(p.target_kw||0).toFixed(2)} kW</div>
                  <div className="text-xs text-slate-500">Ziel‑Leistung</div>
                  <div className="mt-1 text-sm">Ist: <strong>{p.current_kw!=null ? Number(p.current_kw).toFixed(2) : "—"}</strong> kW</div>
                </div>
              </div>

              <div className="mt-3 flex flex-wrap gap-2">
                <button onClick={()=>doSetMode(p.id,"eco")} className="px-3 py-1 bg-emerald-600 text-white rounded">Eco</button>
                <button onClick={()=>doSetMode(p.id,"price")} className="px-3 py-1 bg-cyan-700 text-white rounded">Price</button>
                <button onClick={()=>doSetMode(p.id,"max")} className="px-3 py-1 bg-indigo-600 text-white rounded">Max</button>
                <button onClick={()=>doSetMode(p.id,"off")} className="px-3 py-1 bg-slate-600 text-white rounded">Aus</button>
                <button onClick={()=>doSetLimit(p.id)} className="px-3 py-1 bg-amber-600 text-white rounded">kW setzen (manuell)</button>
              </div>

              <BoostPanel point={p} onSaved={load}/>
            </div>
          ))}
          {points.length===0 && !loading && <div className="text-slate-500">Noch keine Wallbox verbunden…</div>}
        </div>
      </main>
    </div>
  )
}
