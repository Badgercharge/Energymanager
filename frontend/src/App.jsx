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

function StatusPill({ status, label }) {
  const color = useMemo(() => {
    const s = (status || "").toLowerCase();
    if (s.includes("fault")) return "#ef4444";
    if (s.includes("charging")) return "#10b981";
    if (s.includes("suspend")) return "#f59e0b";
    if (s.includes("available")) return "#3b82f6";
    return "#6b7280";
  }, [status]);
  return (
    <span style={{ padding: "2px 8px", borderRadius: 999, background: color, color: "white", fontSize: 12 }}>
      {label || status || "Unbekannt"}
    </span>
  );
}

function Num({ value, unit, digits = 2 }) {
  if (value === null || value === undefined) return <span>–</span>;
  const n = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(n)) return <span>–</span>;
  return (
    <span>
      {n.toFixed(digits)} {unit || ""}
    </span>
  );
}

function Section({ title, children, right }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <h2 style={{ margin: 0, fontSize: 18 }}>{title}</h2>
        {right}
      </div>
      <div style={{ border: "1px solid #e5e7eb", borderRadius: 8, padding: 12, background: "white" }}>{children}</div>
    </div>
  );
}

function PriceBadge({ price }) {
  if (!price) return null;
  const { current_ct_per_kwh, median_ct_per_kwh, below_or_equal_median } = price;
  const bg = below_or_equal_median === true ? "#e6fffa" : below_or_equal_median === false ? "#fff7ed" : "#f3f4f6";
  const fg = below_or_equal_median === true ? "#0f766e" : below_or_equal_median === false ? "#9a3412" : "#374151";
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "6px 10px", borderRadius: 8, background: bg, color: fg }}>
      <strong>Preis:</strong>
      <span>
        <Num value={current_ct_per_kwh} unit="ct/kWh" digits={2} />
      </span>
      <span style={{ fontSize: 12, opacity: 0.8 }}>
        (Median <Num value={median_ct_per_kwh} unit="ct/kWh" digits={2} />)
      </span>
      <span style={{ fontWeight: 600 }}>
        {below_or_equal_median === true ? "günstig" : below_or_equal_median === false ? "teuer" : "n/a"}
      </span>
    </div>
  );
}

function WeatherBadge({ weather }) {
  if (!weather) return null;
  const { cloud_cover, shortwave_radiation, temperature_2m } = weather || {};
  return (
    <div style={{ display: "inline-flex", alignItems: "center", gap: 12, padding: "6px 10px", borderRadius: 8, background: "#f8fafc" }}>
      <span>Wolkendecke: <Num value={cloud_cover} unit="%" digits={0} /></span>
      <span>Globalstrahlung: <Num value={shortwave_radiation} unit="W/m²" digits={0} /></span>
      <span>Temp: <Num value={temperature_2m} unit="°C" digits={1} /></span>
    </div>
  );
}

function ChargePointCard({ p, apiBase }) {
  const [boost, setBoost] = useState(null);
  const [boostErr, setBoostErr] = useState(null);
  const [hideBoost, setHideBoost] = useState(false);

  const loadBoost = async () => {
    try {
      const res = await fetch(`${apiBase}/api/points/${p.id}/boost`);
      if (res.status === 404) {
        setHideBoost(true);
        return;
      }
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const json = await res.json();
      setBoost(json);
      setHideBoost(false);
      setBoostErr(null);
    } catch (e) {
      setBoostErr(e.message || String(e));
    }
  };

  useEffect(() => {
    if (p?.id) loadBoost();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p?.id]);

  const updateBoost = async (next) => {
    try {
      const res = await fetch(`${apiBase}/api/points/${p.id}/boost`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(next),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const json = await res.json();
      setBoost(json);
      setBoostErr(null);
    } catch (e) {
      setBoostErr(e.message || String(e));
    }
  };

  const toggleBoost = async () => {
    if (!boost) return;
    await updateBoost({ ...boost, enabled: !boost.enabled });
  };

  const setBoostKw = async (kw) => {
    if (!boost) return;
    const clamped = Math.max(3.7, Math.min(11.0, Number(kw)));
    await updateBoost({ ...boost, kw: clamped, enabled: boost.enabled });
  };

  const session = p.session || {};
  const fmtTs = (ts) => (ts ? new Date(ts).toLocaleString() : "–");

  return (
    <div style={{ border: "1px solid #e5e7eb", borderRadius: 10, padding: 14, background: "white" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div>
          <div style={{ fontWeight: 600, fontSize: 16 }}>{p.id}</div>
          <div style={{ color: "#6b7280", fontSize: 12 }}>{p.vendor || "—"} {p.model || ""}</div>
        </div>
        <StatusPill status={p.status} label={p.status_label} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
        <div>
          <div style={{ color: "#6b7280", fontSize: 12 }}>Leistung</div>
          <div style={{ fontSize: 20, fontWeight: 600 }}>
            <Num value={p.power_kw} unit="kW" digits={2} />
          </div>
        </div>
        <div>
          <div style={{ color: "#6b7280", fontSize: 12 }}>Ziel-Leistung</div>
          <div style={{ fontSize: 20, fontWeight: 600 }}>
            <Num value={p.target_kw} unit="kW" digits={2} />
          </div>
        </div>
        <div>
          <div style={{ color: "#6b7280", fontSize: 12 }}>Energie (Session)</div>
          <div style={{ fontSize: 20, fontWeight: 600 }}>
            <Num value={p.energy_kwh_session} unit="kWh" digits={3} />
          </div>
        </div>
        <div>
          <div style={{ color: "#6b7280", fontSize: 12 }}>SoC</div>
          <div style={{ fontSize: 20, fontWeight: 600 }}>{p.soc ?? "–"}%</div>
        </div>
      </div>

      <div style={{ marginTop: 10, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
        <div style={{ color: "#374151", fontSize: 12 }}>
          <div>Session Start: {fmtTs(session.start)}</div>
          <div>Last seen: {fmtTs(p.last_seen)}</div>
          <div>Tx aktiv: {p.tx_active ? "ja" : "nein"}</div>
        </div>

        {!hideBoost && (
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <strong>Boost</strong>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <input
                  type="checkbox"
                  checked={!!(boost && boost.enabled)}
                  onChange={toggleBoost}
                />
                aktiv
              </label>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="range"
                min={3.7}
                max={11.0}
                step={0.1}
                value={boost?.kw ?? 11.0}
                onChange={(e) => setBoostKw(e.target.value)}
                style={{ width: 180 }}
              />
              <input
                type="number"
                min={3.7}
                max={11.0}
                step={0.1}
                value={boost?.kw ?? 11.0}
                onChange={(e) => setBoostKw(e.target.value)}
                style={{ width: 90 }}
              />
              <span>kW</span>
            </div>
            {boostErr && <div style={{ color: "#b91c1c", fontSize: 12, marginTop: 6 }}>{boostErr}</div>}
            {!boost && <div style={{ color: "#6b7280", fontSize: 12 }}>Lade Boost-Status …</div>}
          </div>
        )}
      </div>
    </div>
  );
}

export default function App() {
  const { data: points, error: pointsErr } = usePoll(`${API_BASE}/api/points`, 2000, []);
  const { data: price, error: priceErr } = usePoll(`${API_BASE}/api/price`, 60000, null);
  const { data: weather, error: weatherErr } = usePoll(`${API_BASE}/api/weather`, 60000, null);
  const { data: stats } = usePoll(`${API_BASE}/api/stats`, 5000, null);

  return (
    <div style={{ maxWidth: 1000, margin: "20px auto", padding: "0 16px", fontFamily: "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif", color: "#111827" }}>
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <h1 style={{ margin: 0, fontSize: 22 }}>HomeCharger</h1>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
          <PriceBadge price={price} />
          <WeatherBadge weather={weather} />
        </div>
      </header>

      {(priceErr || weatherErr || pointsErr) && (
        <div style={{ background: "#fef2f2", border: "1px solid #fecaca", color: "#7f1d1d", padding: 8, borderRadius: 8, marginBottom: 12, fontSize: 14 }}>
          {priceErr && <div>Preis-API Fehler: {priceErr}</div>}
          {weatherErr && <div>Wetter-API Fehler: {weatherErr}</div>}
          {pointsErr && <div>Backend Fehler (/api/points): {pointsErr}</div>}
        </div>
      )}

      <Section
        title="Ladepunkte"
        right={<div style={{ color: "#6b7280", fontSize: 12 }}>
          {stats ? <>aktiv: {stats.charging} · Gesamtleistung: <Num value={stats.total_power_kw} unit="kW" digits={2} /></> : "–"}
        </div>}
      >
        {Array.isArray(points) && points.length > 0 ? (
          <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
            {points.map((p) => (
              <ChargePointCard key={p.id} p={p} apiBase={API_BASE} />
            ))}
          </div>
        ) : (
          <div style={{ color: "#6b7280" }}>
            Noch keine Ladestation verbunden. Stelle in der Wallbox die WebSocket-URL auf
            <div style={{ fontFamily: "monospace" }}>wss://homecharger.onrender.com/ocpp/DEINE-CP-ID</div>
          </div>
        )}
      </Section>

      <Section title="Live Logs">
        <LiveLogs apiBase={API_BASE} />
      </Section>

      <footer style={{ marginTop: 24, color: "#9ca3af", fontSize: 12 }}>
        API: {API_BASE}
      </footer>
    </div>
  );
}
