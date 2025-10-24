import { useEffect, useMemo, useState } from "react";
import LiveLogs from "./components/LiveLogs";

const API_BASE = import.meta.env.VITE_API_BASE || "https://homecharger.onrender.com";

function usePoll(url, intervalMs, initial = null) {
  const [data, setData] = useState(initial);
  const [error, setError] = useState(null);
  useEffect(() => {
    let stop = false;
    let timer;
    const tick = async () => {
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const json = await res.json();
        if (!stop) setData(json);
        setError(null);
      } catch (e) {
        if (!stop) setError(e.message || String(e));
      } finally {
        if (!stop) timer = setTimeout(tick, intervalMs);
      }
    };
    tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [url, intervalMs]);
  return { data, error };
}

function formatKw(v) {
  if (v == null) return "-";
  return `${Number(v).toFixed(2)} kW`;
}
function formatPct(v) {
  if (v == null) return "-";
  return `${v}%`;
}
function formatTime(iso) {
  if (!iso) return "-";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

export default function App() {
  const { data: points } = usePoll(`${API_BASE}/api/points`, 2000, []);
  const { data: price } = usePoll(`${API_BASE}/api/price`, 60000, null);
  const { data: weather } = usePoll(`${API_BASE}/api/weather`, 300000, null);
  const [manualKw, setManualKw] = useState("11");

  const currentCt = price?.current_ct_per_kwh;
  const medianCt = price?.median_ct_per_kwh;
  const cheaper = useMemo(() => {
    if (currentCt == null || medianCt == null) return null;
    return currentCt <= medianCt;
  }, [currentCt, medianCt]);

  async function sendLimit(cpId, kw) {
    const res = await fetch(`${API_BASE}/api/points/${cpId}/limit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kw }),
    });
    if (!res.ok) {
      const t = await res.text().catch(() => "");
      alert(`Limit-Setzen fehlgeschlagen: ${res.status} ${t}`);
    }
  }

  async function sendBoost(cpId, kw = 11) {
    const res = await fetch(`${API_BASE}/api/points/${cpId}/boost`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kw }),
    });
    if (!res.ok) {
      const t = await res.text().catch(() => "");
      alert(`Boost fehlgeschlagen: ${res.status} ${t}`);
    }
  }

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: 16, fontFamily: "system-ui, sans-serif" }}>
      <h1 style={{ marginBottom: 8 }}>Home Charger EMS</h1>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12, marginBottom: 16 }}>
        <div style={{ border: "1px solid #e5e7eb", borderRadius: 8, padding: 12 }}>
          <h3>Strompreis</h3>
          <div>Aktuell: {currentCt != null ? `${currentCt.toFixed(2)} ct/kWh` : "-"}</div>
          <div>Median 24h: {medianCt != null ? `${medianCt.toFixed(2)} ct/kWh` : "-"}</div>
          <div>Status: {cheaper == null ? "-" : cheaper ? "günstig (≤ Median)" : "teuer (> Median)"}</div>
        </div>
        <div style={{ border: "1px solid #e5e7eb", borderRadius: 8, padding: 12 }}>
          <h3>Wetter</h3>
          <div>Bewölkung: {weather?.cloud_cover ?? "-"}%</div>
          <div>Globalstrahlung: {weather?.shortwave_radiation ?? "-"} W/m²</div>
          <div>Temp: {weather?.temperature_2m ?? "-"} °C</div>
        </div>
      </div>

      <h2 style={{ marginTop: 8 }}>Ladepunkte</h2>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12 }}>
        {(points || []).map((p) => (
          <div key={p.id} style={{ border: "1px solid #e5e7eb", borderRadius: 8, padding: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
              <strong>{p.id}</strong>
              <span style={{ fontSize: 12, opacity: 0.7 }}>{formatTime(p.last_seen)}</span>
            </div>
            <div style={{ marginTop: 6, fontSize: 14, color: "#4b5563" }}>
              <div>Status: <strong>{p.status ?? "-"}</strong> {p.error_code && p.error_code !== "NoError" ? `(Err: ${p.error_code})` : ""}</div>
              <div>Leistung: <strong>{formatKw(p.power_kw)}</strong></div>
              <div>Session kWh: <strong>{p.energy_kwh_session != null ? p.energy_kwh_session.toFixed(3) : "-"}</strong></div>
              <div>SoC: <strong>{formatPct(p.soc)}</strong></div>
              <div>Ziel-Leistung: <strong>{formatKw(p.target_kw)}</strong></div>
              <div>Modell: {p.vendor ? `${p.vendor} ${p.model || ""}` : "-"}</div>
              {p.last_profile_status && <div>Profile push: {p.last_profile_status}</div>}
            </div>

            <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
              <button
                onClick={() => sendBoost(p.id, 11)}
                style={{ padding: "6px 10px", borderRadius: 6, border: "1px solid #d1d5db", background: "#111827", color: "white" }}
              >
                Boost 11 kW
              </button>

              <input
                type="number"
                min="0"
                step="0.1"
                value={manualKw}
                onChange={(e) => setManualKw(e.target.value)}
                style={{ width: 90, padding: "6px 8px", border: "1px solid #d1d5db", borderRadius: 6 }}
                placeholder="kW"
              />
              <button
                onClick={() => sendLimit(p.id, Number(manualKw))}
                style={{ padding: "6px 10px", borderRadius: 6, border: "1px solid #d1d5db", background: "white" }}
              >
                kW setzen
              </button>
            </div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 24 }}>
        <h2 style={{ marginBottom: 8 }}>Live-Logs</h2>
        <LiveLogs apiBase={API_BASE} />
      </div>
    </div>
  );
}
