import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import client from "../api/client";
import Layout from "../components/Layout.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import { useLiveEvents } from "../hooks/useLiveEvents.js";

const TABS = ["Jobs", "Dead letter", "Recurring", "Stats"];

export default function QueueDetail() {
  const { projectId, queueId } = useParams();
  const [tab, setTab] = useState("Jobs");
  const [jobs, setJobs] = useState({ items: [] });
  const [statusFilter, setStatusFilter] = useState("");
  const [dlq, setDlq] = useState([]);
  const [schedules, setSchedules] = useState([]);
  const [stats, setStats] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [jobDetail, setJobDetail] = useState(null);
  const [newJob, setNewJob] = useState({ name: "", job_type: "immediate", payload: "{}" });
  const [newCron, setNewCron] = useState({ name: "", cron_expression: "*/5 * * * *", payload: "{}" });

  const { connected } = useLiveEvents(() => {
    if (tab === "Jobs") loadJobs();
  });

  async function loadJobs() {
    const params = statusFilter ? { status: statusFilter } : {};
    const res = await client.get(`/api/v1/queues/${queueId}/jobs`, { params });
    setJobs(res.data);
  }
  async function loadDlq() {
    setDlq((await client.get(`/api/v1/queues/${queueId}/dead-letter`)).data);
  }
  async function loadSchedules() {
    setSchedules((await client.get(`/api/v1/queues/${queueId}/scheduled-jobs`)).data);
  }
  async function loadStats() {
    setStats((await client.get(`/api/v1/projects/${projectId}/queues/${queueId}/stats`)).data);
  }

  useEffect(() => {
    if (tab === "Jobs") loadJobs();
    if (tab === "Dead letter") loadDlq();
    if (tab === "Recurring") loadSchedules();
    if (tab === "Stats") loadStats();
    // eslint-disable-next-line
  }, [tab, statusFilter, queueId]);

  async function expandJob(jobId) {
    if (expanded === jobId) { setExpanded(null); return; }
    const res = await client.get(`/api/v1/queues/${queueId}/jobs/${jobId}`);
    setJobDetail(res.data);
    setExpanded(jobId);
  }

  async function submitJob(e) {
    e.preventDefault();
    let payload = {};
    try { payload = JSON.parse(newJob.payload || "{}"); } catch { /* ignore invalid json, send empty */ }
    await client.post(`/api/v1/queues/${queueId}/jobs`, {
      name: newJob.name,
      job_type: newJob.job_type,
      payload,
      run_at: newJob.run_at ? new Date(newJob.run_at).toISOString() : null,
    });
    setNewJob({ name: "", job_type: "immediate", payload: "{}" });
    loadJobs();
  }

  async function cancelJob(jobId) {
    await client.post(`/api/v1/queues/${queueId}/jobs/${jobId}/cancel`);
    loadJobs();
  }

  async function requeue(entryId) {
    await client.post(`/api/v1/queues/${queueId}/dead-letter/${entryId}/requeue`);
    loadDlq();
  }

  async function createCron(e) {
    e.preventDefault();
    let payload = {};
    try { payload = JSON.parse(newCron.payload || "{}"); } catch { /* ignore */ }
    await client.post(`/api/v1/queues/${queueId}/scheduled-jobs`, { ...newCron, payload });
    setNewCron({ name: "", cron_expression: "*/5 * * * *", payload: "{}" });
    loadSchedules();
  }

  return (
    <Layout title={`Queue #${queueId}`} subtitle="Job explorer, execution history, retries and recurring schedules" connected={connected}>
      <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
        {TABS.map((t) => (
          <button key={t} className="btn btn-sm" onClick={() => setTab(t)} style={tab === t ? { borderColor: "var(--accent)", color: "var(--accent)" } : undefined}>
            {t}
          </button>
        ))}
      </div>

      {tab === "Jobs" && (
        <>
          <form className="card" onSubmit={submitJob} style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 16 }}>
            <div className="field" style={{ marginBottom: 0, minWidth: 160 }}>
              <label>Job name</label>
              <input value={newJob.name} onChange={(e) => setNewJob({ ...newJob, name: e.target.value })} required />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 150 }}>
              <label>Type</label>
              <select value={newJob.job_type} onChange={(e) => setNewJob({ ...newJob, job_type: e.target.value })}>
                <option value="immediate">Immediate</option>
                <option value="delayed">Delayed</option>
                <option value="scheduled">Scheduled</option>
              </select>
            </div>
            {newJob.job_type !== "immediate" && (
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Run at</label>
                <input type="datetime-local" value={newJob.run_at || ""} onChange={(e) => setNewJob({ ...newJob, run_at: e.target.value })} required />
              </div>
            )}
            <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
              <label>Payload (JSON)</label>
              <input value={newJob.payload} onChange={(e) => setNewJob({ ...newJob, payload: e.target.value })} />
            </div>
            <button className="btn btn-primary" type="submit">Submit job</button>
          </form>

          <div style={{ marginBottom: 12 }}>
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={{ width: 200 }}>
              <option value="">All statuses</option>
              {["queued", "scheduled", "claimed", "running", "completed", "failed", "retry_scheduled", "dead_letter", "cancelled"].map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className="card">
            <table>
              <thead>
                <tr><th>ID</th><th>Name</th><th>Status</th><th>Attempts</th><th>Run at</th><th></th></tr>
              </thead>
              <tbody>
                {jobs.items.map((j) => (
                  <>
                    <tr key={j.id} onClick={() => expandJob(j.id)} style={{ cursor: "pointer" }}>
                      <td className="mono">#{j.id}</td>
                      <td>{j.name}</td>
                      <td><StatusBadge status={j.status} /></td>
                      <td className="mono">{j.attempt_count}/{j.max_retries}</td>
                      <td className="mono">{new Date(j.run_at).toLocaleString()}</td>
                      <td>
                        {["queued", "scheduled", "retry_scheduled"].includes(j.status) && (
                          <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); cancelJob(j.id); }}>Cancel</button>
                        )}
                      </td>
                    </tr>
                    {expanded === j.id && jobDetail && (
                      <tr>
                        <td colSpan={6} style={{ background: "var(--surface-raised)" }}>
                          <div className="mono" style={{ marginBottom: 8 }}>Execution history</div>
                          {jobDetail.executions.length === 0 && <div className="page-subtitle">No attempts yet.</div>}
                          {jobDetail.executions.map((ex) => (
                            <div key={ex.id} style={{ display: "flex", gap: 16, padding: "6px 0", fontSize: 12 }} className="mono">
                              <span>attempt {ex.attempt_number}</span>
                              <StatusBadge status={ex.status} />
                              <span>{ex.duration_ms != null ? `${ex.duration_ms}ms` : "—"}</span>
                              {ex.error_message && <span style={{ color: "var(--danger)" }}>{ex.error_message}</span>}
                              {ex.next_retry_at && <span>next retry: {new Date(ex.next_retry_at).toLocaleTimeString()}</span>}
                            </div>
                          ))}
                        </td>
                      </tr>
                    )}
                  </>
                ))}
                {jobs.items.length === 0 && <tr><td colSpan={6} className="page-subtitle">No jobs found.</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === "Dead letter" && (
        <div className="card">
          <table>
            <thead><tr><th>Entry</th><th>Job</th><th>Attempts</th><th>Error</th><th>Failed at</th><th></th></tr></thead>
            <tbody>
              {dlq.map((d) => (
                <tr key={d.id}>
                  <td className="mono">#{d.id}</td>
                  <td className="mono">job #{d.job_id}</td>
                  <td className="mono">{d.attempt_count}</td>
                  <td style={{ color: "var(--danger)" }}>{d.final_error}</td>
                  <td className="mono">{new Date(d.created_at).toLocaleString()}</td>
                  <td>{!d.requeued_at && <button className="btn btn-sm" onClick={() => requeue(d.id)}>Requeue</button>}</td>
                </tr>
              ))}
              {dlq.length === 0 && <tr><td colSpan={6} className="page-subtitle">Dead letter queue is empty.</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {tab === "Recurring" && (
        <>
          <form className="card" onSubmit={createCron} style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 16 }}>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Name</label>
              <input value={newCron.name} onChange={(e) => setNewCron({ ...newCron, name: e.target.value })} required />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Cron expression</label>
              <input value={newCron.cron_expression} onChange={(e) => setNewCron({ ...newCron, cron_expression: e.target.value })} required />
            </div>
            <div className="field" style={{ marginBottom: 0, flex: 1 }}>
              <label>Payload (JSON)</label>
              <input value={newCron.payload} onChange={(e) => setNewCron({ ...newCron, payload: e.target.value })} />
            </div>
            <button className="btn btn-primary" type="submit">Create schedule</button>
          </form>
          <div className="card">
            <table>
              <thead><tr><th>Name</th><th>Cron</th><th>Next run</th><th>Last run</th><th>Active</th></tr></thead>
              <tbody>
                {schedules.map((s) => (
                  <tr key={s.id}>
                    <td>{s.name}</td>
                    <td className="mono">{s.cron_expression}</td>
                    <td className="mono">{new Date(s.next_run_at).toLocaleString()}</td>
                    <td className="mono">{s.last_run_at ? new Date(s.last_run_at).toLocaleString() : "—"}</td>
                    <td>{s.is_active ? "yes" : "paused"}</td>
                  </tr>
                ))}
                {schedules.length === 0 && <tr><td colSpan={5} className="page-subtitle">No recurring schedules.</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === "Stats" && stats && (
        <div className="grid grid-4">
          <MetricCard label="Queued" value={stats.queued} />
          <MetricCard label="Running" value={stats.running} accent="var(--accent)" />
          <MetricCard label="Completed" value={stats.completed} accent="var(--success)" />
          <MetricCard label="Dead letter" value={stats.dead_letter} accent="var(--danger)" />
          <MetricCard label="Avg duration" value={stats.avg_duration_ms ? `${Math.round(stats.avg_duration_ms)}ms` : "—"} />
          <MetricCard label="Throughput (1h)" value={stats.throughput_last_hour} />
        </div>
      )}
    </Layout>
  );
}

function MetricCard({ label, value, accent }) {
  return (
    <div className="card">
      <div className="metric-label">{label}</div>
      <div className="metric-value" style={accent ? { color: accent } : undefined}>{value}</div>
    </div>
  );
}