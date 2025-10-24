import { useEffect, useRef, useState } from "react";

export default function LiveLogs({ apiBase }) {
  const [items, setItems] = useState([]);
  const boxRef = useRef(null);

  useEffect(() => {
    let timer;
    const fetchLogs = async () => {
      try {
        const res = await fetch(`${apiBase}/api/logs?limit=200`);
        const data = await res.json();
        setItems(data.items || []);
        if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
      } catch (e) {
        // ignore transient errors
      } finally {
        timer = setTimeout(fetchLogs, 2000);
      }
    };
    fetchLogs();
    return () => timer && clearTimeout(timer);
  }, [apiBase]);

  return (
    <div style={{ border: "1px solid #ddd", borderRadius: 8, padding: 8 }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>Live Logs</div>
      <div ref={boxRef} style={{ height: 200, overflowY: "auto", fontFamily: "monospace", fontSize: 12, whiteSpace: "pre-wrap" }}>
        {items.map((r, i) => (
          <div key={i}>
            [{r.ts}] {r.level} {r.logger}: {r.msg}
          </div>
        ))}
      </div>
    </div>
  );
}
