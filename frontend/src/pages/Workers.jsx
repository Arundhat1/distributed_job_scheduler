import { useEffect, useState } from "react";
import client from "../api/client";
import Layout from "../components/Layout.jsx";
import { useLiveEvents } from "../hooks/useLiveEvents.js";

export default function Workers() {
  const [workers, setWorkers] = useState([]);
  const [pulses, setPulses] = useState({}); // worker_id -> array of recent tick heights

  const { connected } = useLiveEvents((envelope) => {
    if (envelope.type === "jobs_claimed" || envelope.type === "job_started") {
      const wid = envelope.data.worker_id;
      setPulses((prev) => {
        const ticks = [...(prev[wid] || Array(20).fill(0.3)), 1];
        return { ...prev, [wid]: ticks.slice(-20) };
      });
    }
  });

  async function load() {
    setWorkers((await client.get("/api/v1/workers")).data);
  }

  useEffect(() => {
    load();
    const id = setInterval(load, 8000);
    return () => clearInterval(id);
  }, []);

  function isStale(worker) {
    if (!worker.last_heartbeat_at) return true;
    return Date.now() - new Date(worker.last_heartbeat_at).getTime() > 30000;
  }

  return (
    <Layout title="Workers" subtitle="Registered worker processes and live heartbeat activity" connected={connected}>
      <div className="grid grid-2">
        {workers.map((w) => {
          const stale = isStale(w) || w.status === "dead";
          const ticks = pulses[w.id] || Array(20).fill(0.3);
          return (
            <div key={w.id} className="card" style={{ borderLeft: `3px solid ${stale ? "var(--danger)" : "var(--success)"}` }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div>
                  <div style={{ fontFamily: "var(--font-mono)", fontSize: 15 }}>{w.name}</div>
                  <div className="page-subtitle">{w.hostname} · pid {w.pid}</div>
                </div>
                <span className={`badge badge-${stale ? "dead_letter" : w.status === "busy" ? "running" : "completed"}`}>
                  <span className="dot" /> {stale ? "dead" : w.status}
                </span>
              </div>

              <div style={{ marginTop: 14, display: "flex", justifyContent: "space-between", fontSize: 12 }} className="mono">
                <span>queues: {w.queues}</span>
                <span>{w.current_job_count}/{w.concurrency_limit} active</span>
              </div>

              <div className="worker-strip" style={{ marginTop: 12 }}>
                {ticks.map((h, i) => (
                  <div key={i} className={"tick" + (i === ticks.length - 1 ? " recent" : "")} style={{ height: `${h * 24}px` }} />
                ))}
              </div>
              <div className="page-subtitle" style={{ marginTop: 8 }}>
                last heartbeat: {w.last_heartbeat_at ? new Date(w.last_heartbeat_at).toLocaleTimeString() : "never"}
              </div>
            </div>
          );
        })}
        {workers.length === 0 && <div className="empty-state">No workers have registered yet. Start one with scripts/run_worker.py.</div>}
      </div>
    </Layout>
  );
}