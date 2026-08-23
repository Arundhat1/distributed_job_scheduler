import { useEffect, useRef, useState } from "react";
import { wsUrl } from "../api/client";

/**
 * Connects to the dashboard WebSocket and returns the most recent event
 * plus a rolling connection status, reconnecting with backoff if the
 * socket drops. Consumers pass an onEvent callback rather than reading
 * every message from state, so a busy job feed doesn't cause a re-render
 * storm in components that only care about the connection dot.
 */
export function useLiveEvents(onEvent) {
  const [connected, setConnected] = useState(false);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    let socket;
    let retryDelay = 1000;
    let cancelled = false;

    function connect() {
      socket = new WebSocket(wsUrl());
      socket.onopen = () => {
        setConnected(true);
        retryDelay = 1000;
      };
      socket.onmessage = (event) => {
        try {
          onEventRef.current?.(JSON.parse(event.data));
        } catch {
          /* ignore malformed frame */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (!cancelled) setTimeout(connect, Math.min(retryDelay *= 1.5, 10000));
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