import { useEffect, useRef, useState } from "react";
import { wsUrl } from "../api/client";

/**
 * Connects to the authenticated dashboard WebSocket. Every message is a
 * structured envelope { id, type, ts, data } (see backend
 * app/websocket_manager.py). On reconnect, passes ?since=<last seen id>
 * so the server can replay its best-effort backlog — this is a
 * convenience, not a durability guarantee, so consumers should still
 * treat REST (/dashboard/summary) as the source of truth and this feed
 * as a low-latency nice-to-have on top of it.
 */
export function useLiveEvents(onEvent) {
  const [connected, setConnected] = useState(false);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const lastEventId = useRef(null);

  useEffect(() => {
    let socket;
    let retryDelay = 1000;
    let cancelled = false;

    function connect() {
      const token = localStorage.getItem("token");
      if (!token) return; // not logged in yet; nothing to authenticate the socket with

      socket = new WebSocket(wsUrl(lastEventId.current));
      socket.onopen = () => {
        setConnected(true);
        retryDelay = 1000;
      };
      socket.onmessage = (event) => {
        try {
          const envelope = JSON.parse(event.data);
          if (typeof envelope.id === "number") lastEventId.current = envelope.id;
          onEventRef.current?.(envelope);
        } catch {
          /* ignore malformed frame */
        }
      };
      socket.onclose = (event) => {
        setConnected(false);
        // 1008 = policy violation (bad/expired token) — don't hammer retries, the
        // token needs a fresh login, not a reconnect.
        if (event.code === 1008) return;
        if (!cancelled) setTimeout(connect, Math.min((retryDelay *= 1.5), 10000));
      };
      socket.onerror = () => socket.close();
    }
    connect();

    const keepAlive = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
    }, 20000);

    return () => {
      cancelled = true;
      clearInterval(keepAlive);
      socket?.close();
    };
  }, []);

  return { connected };
}