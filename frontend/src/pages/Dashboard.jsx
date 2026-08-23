import { useEffect, useState } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import client from "../api/client";
import Layout from "../components/Layout.jsx";
import { useLiveEvents } from "../hooks/useLiveEvents.js";

export default function Dashboard() {
  const [summary, setSummary] = useState(null);
  const [events, setEvents] = useState([]);

  const { connected } = useLiveEvents((evt) => {
    setEvents((prev) => [{ ...evt, ts: new Date().toLocaleTimeString() }, ...prev].slice(0, 30));
  });

  async function load() {
    const res = await client.get("/api/v1/dashboard/summary");
    setSummary(res.data);
  }

  useEffect(() => {
    load();
    const id = setInterval(load, 10000); // periodic refresh backstops the WS event feed
    return () => clearInterval(id);
  }, []);

  const counts = summary?.job_counts || {};
  const chartData = (summary?.throughput_by_hour || []).map((p) => ({
    hour: new Date(p.hour).getHours() + ":00",
    completed: p.count,
  }));

  return (
    <Layout title="Overview" subtitle="System-wide job and worker health" connected={connected}>
      <div className="grid grid-4">
        <MetricCard label="Queued" value={counts.queued || 0} />
        <MetricCard label="Running" value={counts.running || 0} accent="var(--accent)" />
        <MetricCard label="Completed (all-time)" value={counts.completed || 0} accent="var(--success)" />
        <MetricCard label="Dead letter" value={counts.dead_letter || 0} accent="var(--danger)" />
      </div>

      <div className="section-title">Throughput — completed jobs / hour (last 24h)</div>
      <div className="card" style={{ height: 220 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData}>
            <CartesianGrid stroke="#232933" vertical={false} />
            <XAxis dataKey="hour" stroke="#7C8698" fontSize={12} tickLine={false} axisLine={false} />
            <YAxis stroke="#7C8698" fontSize={12} tickLine={false} axisLine={false} allowDecimals={false} />
            <Tooltip contentStyle={{ background: "#181d25", border: "1px solid #232933", borderRadius: 6, fontSize: 12 }} />
            <Line type="monotone" dataKey="completed" stroke="#35C989" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="section-title">Live activity</div>
      <div className="card" style={{ maxHeight: 280, overflowY: "auto" }}>
        {events.length === 0 && <div className="empty-state">Waiting for job or worker events…</div>}
        {events.map((e, i) => (
          <div key={i} style={{ display: "flex", gap: 12, padding: "6px 0", borderBottom: "1px solid var(--border)", fontSize: 13 }}>
            <span className="mono">{e.ts}</span>
            <span>{describeEvent(e)}</span>
          </div>
        ))}
      </div>
    </Layout>
  );
}

function describeEvent(e) {
  switch (e.type) {
    case "jobs_claimed":
      return `worker #${e.worker_id} claimed ${e.job_ids?.length} job(s)`;
    case "job_started":
      return `job #${e.job_id} started on worker #${e.worker_id}`;
    case "job_finished":
      return `job #${e.job_id} finished → ${e.status}`;
    case "worker_registered":
      return `worker "${e.name}" registered`;
    default:
      return JSON.stringify(e);
  }
}

function MetricCard({ label, value, accent }) {
  return (
    <div className="card">
      <div className="metric-label">{label}</div>
      <div className="metric-value" style={accent ? { color: accent } : undefined}>
        {value}
      </div>
    </div>
  );
}