import { Navigate, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard.jsx";
import Login from "./pages/Login.jsx";
import QueueDetail from "./pages/QueueDetail.jsx";
import Queues from "./pages/Queues.jsx";
import Workers from "./pages/Workers.jsx";

function RequireAuth({ children }) {
  const token = localStorage.getItem("token");
  return token ? children : <Navigate to="/login" replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<RequireAuth><Dashboard /></RequireAuth>} />
      <Route path="/queues" element={<RequireAuth><Queues /></RequireAuth>} />
      <Route path="/queues/:projectId/:queueId" element={<RequireAuth><QueueDetail /></RequireAuth>} />
      <Route path="/workers" element={<RequireAuth><Workers /></RequireAuth>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}