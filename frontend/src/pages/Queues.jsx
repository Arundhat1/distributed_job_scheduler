import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import client from "../api/client";
import Layout from "../components/Layout.jsx";
import { useLiveEvents } from "../hooks/useLiveEvents.js";

export default function Queues() {
  const [projects, setProjects] = useState([]);
  const [queuesByProject, setQueuesByProject] = useState({});
  const [showNewProject, setShowNewProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [newQueue, setNewQueue] = useState({}); // projectId -> {name, priority, max_concurrency}
  const { connected } = useLiveEvents();

  async function loadAll() {
    const res = await client.get("/api/v1/projects");
    setProjects(res.data);
    const entries = await Promise.all(
      res.data.map(async (p) => [p.id, (await client.get(`/api/v1/projects/${p.id}/queues`)).data])
    );
    setQueuesByProject(Object.fromEntries(entries));
  }

  useEffect(() => {
    loadAll();
  }, []);

  async function createProject(e) {
    e.preventDefault();
    await client.post("/api/v1/projects", { name: newProjectName });
    setNewProjectName("");
    setShowNewProject(false);
    loadAll();
  }

  async function createQueue(projectId, e) {
    e.preventDefault();
    const draft = newQueue[projectId] || {};
    await client.post(`/api/v1/projects/${projectId}/queues`, {
      name: draft.name,
      priority: Number(draft.priority || 0),
      max_concurrency: Number(draft.max_concurrency || 5),
      retry_policy: { name: `${draft.name}-policy`, strategy: draft.strategy || "exponential", base_delay_seconds: 5, multiplier: 2.0, max_delay_seconds: 3600, max_retries: 3 },
    });
    setNewQueue({ ...newQueue, [projectId]: {} });
    loadAll();
  }

  async function togglePause(projectId, queue) {
    const action = queue.is_paused ? "resume" : "pause";
    await client.post(`/api/v1/projects/${projectId}/queues/${queue.id}/${action}`);
    loadAll();
  }

  return (
    <Layout title="Queues" subtitle="Configure queues, priority, concurrency, and retry policy" connected={connected}>
      <div style={{ marginBottom: 16 }}>
        <button className="btn" onClick={() => setShowNewProject((s) => !s)}>
          + New project
        </button>
      </div>

      {showNewProject && (
        <form className="card" onSubmit={createProject} style={{ marginBottom: 24, maxWidth: 420 }}>
          <div className="field">
            <label>Project name</label>
            <input value={newProjectName} onChange={(e) => setNewProjectName(e.target.value)} required autoFocus />
          </div>
          <button className="btn btn-primary" type="submit">Create project</button>
        </form>
      )}

      {projects.map((project) => (
        <div key={project.id} style={{ marginBottom: 32 }}>
          <div className="section-title">{project.name}</div>
          <div className="card" style={{ marginBottom: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>Queue</th>
                  <th>Priority</th>
                  <th>Max concurrency</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {(queuesByProject[project.id] || []).map((q) => (
                  <tr key={q.id}>
                    <td>
                      <Link to={`/queues/${project.id}/${q.id}`} style={{ color: "var(--accent)" }}>{q.name}</Link>
                    </td>
                    <td className="mono">{q.priority}</td>
                    <td className="mono">{q.max_concurrency}</td>
                    <td>{q.is_paused ? <span style={{ color: "var(--warning)" }}>paused</span> : <span style={{ color: "var(--success)" }}>active</span>}</td>
                    <td>
                      <button className="btn btn-sm" onClick={() => togglePause(project.id, q)}>
                        {q.is_paused ? "Resume" : "Pause"}
                      </button>
                    </td>
                  </tr>
                ))}
                {(queuesByProject[project.id] || []).length === 0 && (
                  <tr><td colSpan={5} className="page-subtitle">No queues yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          <form className="card" onSubmit={(e) => createQueue(project.id, e)} style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}>
            <div className="field" style={{ marginBottom: 0, minWidth: 160 }}>
              <label>New queue name</label>
              <input
                value={newQueue[project.id]?.name || ""}
                onChange={(e) => setNewQueue({ ...newQueue, [project.id]: { ...newQueue[project.id], name: e.target.value } })}
                required
              />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 100 }}>
              <label>Priority</label>
              <input type="number" value={newQueue[project.id]?.priority || 0}
                onChange={(e) => setNewQueue({ ...newQueue, [project.id]: { ...newQueue[project.id], priority: e.target.value } })} />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 140 }}>
              <label>Max concurrency</label>
              <input type="number" value={newQueue[project.id]?.max_concurrency || 5}
                onChange={(e) => setNewQueue({ ...newQueue, [project.id]: { ...newQueue[project.id], max_concurrency: e.target.value } })} />
            </div>
            <div className="field" style={{ marginBottom: 0, width: 160 }}>
              <label>Retry strategy</label>
              <select value={newQueue[project.id]?.strategy || "exponential"}
                onChange={(e) => setNewQueue({ ...newQueue, [project.id]: { ...newQueue[project.id], strategy: e.target.value } })}>
                <option value="fixed">Fixed</option>
                <option value="linear">Linear</option>
                <option value="exponential">Exponential</option>
              </select>
            </div>
            <button className="btn btn-primary" type="submit">Add queue</button>
          </form>
        </div>
      ))}
    </Layout>
  );
}