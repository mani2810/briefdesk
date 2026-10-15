// Live connection to one brief: replay-after-reconnect, heartbeat, exponential backoff.
import { api } from "./api.js";

export class BriefSocket {
  constructor(briefId, handlers) {
    this.briefId = briefId;
    this.h = handlers;              // {onEvent, onDelta, onStatus, onRun, onError, onState}
    this.lastId = 0;
    this.closed = false;
    this.attempt = 0;
    this.queue = [];
  }

  async connect() {
    if (this.closed) return;
    this.h.onState?.("connecting");
    let ticket;
    try {
      ({ ticket } = await api(`/briefs/${this.briefId}/ws-ticket`, { method: "POST" }));
    } catch (e) {
      if (e.status === 404 || e.status === 401) { this.h.onState?.("closed"); return; }
      return this.retry();
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/v1/ws/briefs/${this.briefId}?ticket=${encodeURIComponent(ticket)}`);
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "hello", after: this.lastId }));
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      switch (msg.type) {
        case "ready":
          this.attempt = 0;
          this.h.onState?.("live");
          this.h.onReady?.(msg.brief);
          while (this.queue.length) ws.send(this.queue.shift());
          clearInterval(this.ping);
          this.ping = setInterval(() => ws.readyState === 1 && ws.send('{"type":"ping"}'), 25000);
          break;
        case "event":
          this.lastId = Math.max(this.lastId, msg.event.id);
          this.h.onEvent?.(msg.event);
          break;
        case "delta": this.h.onDelta?.(msg); break;
        case "status": this.h.onStatus?.(msg); break;
        case "run": this.h.onRun?.(msg); break;
        case "error": this.h.onError?.(msg.detail); break;
        default: break;
      }
    };
    ws.onclose = (ev) => {
      clearInterval(this.ping);
      if (this.closed || [4401, 4403, 4404].includes(ev.code)) { this.h.onState?.("closed"); return; }
      this.retry();
    };
  }

  retry() {
    if (this.closed) return;
    this.h.onState?.("reconnecting");
    const delay = Math.min(15000, 500 * 2 ** this.attempt++) + Math.random() * 300;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.connect(), delay);
  }

  send(obj) {
    const data = JSON.stringify(obj);
    if (this.ws?.readyState === 1) this.ws.send(data);
    else this.queue.push(data);
  }

  close() {
    this.closed = true;
    clearTimeout(this.timer);
    clearInterval(this.ping);
    this.ws?.close();
  }
}
