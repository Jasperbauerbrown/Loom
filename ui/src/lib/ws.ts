// WebSocket client: one multiplexed socket (03 §12.2), auto-reconnect with 1s→30s
// backoff, re-subscribe on open, full refetch after a reconnect so nothing missed while
// the socket was down stays wrong.

import { token } from "./api";
import type { BusEvent } from "./types";
import type { Store } from "./store";

export class WsClient {
  private socket: WebSocket | null = null;
  private delay = 1000;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private everOpened = false;

  constructor(private store: Store) {}

  url(): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const t = token();
    return `${proto}//${location.host}/ws${t ? `?token=${encodeURIComponent(t)}` : ""}`;
  }

  start() {
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.socket?.close();
    this.socket = null;
  }

  private connect() {
    if (this.stopped) return;
    this.store.setConnection(this.everOpened ? "connecting" : "connecting");
    let socket: WebSocket;
    try {
      socket = new WebSocket(this.url());
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.delay = 1000;
      this.store.setConnection("open");
      socket.send(JSON.stringify({ subscribe: { conversations: "all" } }));
      // A reconnect may have missed events; REST is the source of truth for state.
      if (this.everOpened) void this.store.refreshAll();
      this.everOpened = true;
    };

    socket.onmessage = (msg) => {
      let event: BusEvent;
      try {
        event = JSON.parse(msg.data as string);
      } catch {
        return; // malformed frame: ignore
      }
      if (!event || typeof event.type !== "string") return;
      this.store.dispatch(event);
    };

    socket.onerror = () => {
      /* onclose always follows; nothing to do here */
    };

    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      this.store.setConnection("closed");
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect() {
    if (this.stopped || this.timer) return;
    const wait = this.delay;
    this.delay = Math.min(this.delay * 2, 30000);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, wait);
  }
}
