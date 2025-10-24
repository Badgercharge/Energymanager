import { useEffect, useState } from "react";

export default function LiveLogs({ apiBase, limit = 200 }) {
  const [items, setItems] = useState([]);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let stop = false;
    let timer;
    const tick = async () => {
      try {
        const res = await fetch(`${apiBase}/api/logs?limit=${limit}`);
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const json = await res.json();
        if (!stop) setItems(json);
        setErr(null);
      } catch (e) {
        if (!stop) setErr(e.message || String(e));
      } finally {
        if (!stop) timer = setTimeout(tick, 2000);
      }
    };
    tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [apiBase, limit]);

  return (
    <div style={{
      maxHeight: 260,
      overflow: "auto",
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
      fontSize: 12,
      background: "#0b1020",
      color: "#e5e7eb",
      padding: 8,
      borderRadius: 8,
      border: "1px solid #1f2937"
    }}>
      {err && <div style={{ color: "#fca5a5", marginBottom: 6 }}>Error: {err}</div>}
      {items.length === 0 && !err && <div style={{ opacity: 0.7 }}>Keine Logs</div>}
      {items.map((l, i) => (
        <div key={i} style={{ whiteSpace: "pre-wrap" }}>
          <span style={{ color: "#93c5fd" }}>{l.ts}</span>{" "}
          <span style={{ color: "#fde68a" }}>{(l.level || "").padEnd(5)}</span>{" "}
          <span style={{ color: "#86efac" }}>{l.logger}</span>{" "}
          <span style={{ color: "#e5e7eb" }}>{l.msg}</span>
        </div>
      ))}
    </div>
  );
}
