// The brief view: a live, two-way conversation with the agent team.
import { api, saveFile, downloadFile } from "./api.js";
import { render } from "./markdown.js";
import { BriefSocket } from "./socket.js";
import { ReportPanel } from "./report.js";
import { ACTIVE, AGENTS, bytes, h, icon, icons, statusPill, time, toast } from "./ui.js";

const QUICK = {
  done: ["PDF", "Word", "Slides", "Infographic", "Excel", "Executive summary as PDF"],
  awaiting_approval: ["Looks good, go", "Add a question about risks", "Focus on India"],
  busy: ["Also cover the main competitors", "Keep it short", "Use recent 2025-2026 data"],
};
const FORMAT_ICON = { pdf: "📕", docx: "📘", pptx: "📙", png: "🖼️", jpg: "🖼️", xlsx: "📗", csv: "📗", md: "📝",
  html: "🌐", json: "🧾", txt: "📄" };

export class ChatView {
  constructor(root, brief, { onBriefChange }) {
    this.root = root;
    this.brief = brief;
    this.onBriefChange = onBriefChange;
    this.activity = null;        // the live "agents at work" card of the current run
    this.streaming = null;       // the bubble receiving writer / answer tokens
    this.seen = new Set();
    this.build();
    this.socket = new BriefSocket(brief.id, {
      onReady: (b) => this.update(b),
      onEvent: (e) => this.addEvent(e),
      onDelta: (d) => this.addDelta(d),
      onStatus: (s) => this.update({ status: s.status, ...(s.title ? { title: s.title } : {}) }),
      onRun: (r) => { if (r.status === "finished" && !ACTIVE.has(this.brief.status)) this.closeActivity(); },
      onError: (msg) => toast(msg, "error"),
      onState: (st) => this.conn.setAttribute("data-state", st),
    });
    this.socket.connect();
  }

  destroy() {
    this.socket.close();
    this.panel?.destroy();
  }

  // ------------------------------------------------------------------ layout
  build() {
    this.title = h("h1", { class: "brief-title", title: "Click to rename", tabindex: "0",
      onclick: () => this.rename(), onkeydown: (e) => e.key === "Enter" && this.rename() }, this.brief.title);
    this.pill = h("span");
    this.conn = h("span", { class: "conn", title: "Live connection" });
    this.timeline = h("div", { class: "timeline", role: "log", "aria-live": "polite" });
    this.input = h("textarea", { rows: 1, placeholder: "Message BriefDesk…", "aria-label": "Message",
      oninput: () => this.autosize(), onkeydown: (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.send(); }
      } });
    this.stopBtn = h("button", { class: "btn ghost stop", title: "Stop the agents", onclick: () => this.socket.send({ type: "cancel" }) },
      icon("stop"), "Stop");
    this.chips = h("div", { class: "chips" });
    const composer = h("div", { class: "composer" }, this.chips,
      h("div", { class: "composer-box" }, this.input, this.stopBtn,
        h("button", { class: "send", "aria-label": "Send", onclick: () => this.send(), html: icons.send })),
      h("p", { class: "hint" }, "Type anytime - while agents work, your message steers them. Ask for any format: “as Word”, “slides”, “infographic”…"));
    this.panelBtn = h("button", { class: "icon-btn", title: "Report panel", "aria-label": "Toggle report panel",
      onclick: () => this.root.classList.toggle("panel-open"), html: icons.panel });
    const head = h("header", { class: "brief-head" },
      h("button", { class: "icon-btn mobile-only", "aria-label": "Menu", onclick: () => document.body.classList.toggle("nav-open"), html: icons.menu }),
      h("div", { class: "brief-meta" }, this.title, h("div", { class: "sub" }, this.pill, this.conn,
        h("span", { class: "muted small" }, `${this.brief.depth} · for ${this.brief.audience}`))),
      this.panelBtn);
    this.panelHost = h("aside", { class: "panel" });
    this.root.replaceChildren(h("section", { class: "chat" }, head, h("div", { class: "scroller" }, this.timeline), composer),
      this.panelHost);
    this.panel = new ReportPanel(this.panelHost, this.brief.id, { onCite: () => this.root.classList.add("panel-open") });
    this.update(this.brief);
  }

  autosize() {
    this.input.style.height = "auto";
    this.input.style.height = Math.min(this.input.scrollHeight, 200) + "px";
  }

  update(patch) {
    Object.assign(this.brief, patch);
    this.title.textContent = this.brief.title;
    this.pill.replaceChildren(statusPill(this.brief.status));
    const busy = ACTIVE.has(this.brief.status);
    this.root.classList.toggle("busy", busy);
    const key = busy ? "busy" : this.brief.status === "done" ? "done" : this.brief.status === "awaiting_approval" ? "awaiting_approval" : null;
    this.chips.replaceChildren(...(QUICK[key] || []).map((c) => h("button", { class: "chip", onclick: () => this.send(c) }, c)));
    if (!busy && this.activity) this.closeActivity();
    this.onBriefChange?.(this.brief);
  }

  async rename() {
    const name = prompt("Rename this brief", this.brief.title);
    if (!name || !name.trim()) return;
    try {
      const b = await api(`/briefs/${this.brief.id}`, { method: "PATCH", body: { title: name.trim() } });
      this.update({ title: b.title });
    } catch (e) { toast(e.message, "error"); }
  }

  send(text) {
    const value = (text ?? this.input.value).trim();
    if (!value) return;
    this.socket.send({ type: "message", text: value });
    if (text === undefined) { this.input.value = ""; this.autosize(); }
  }

  scroll(force = false) {
    const sc = this.timeline.parentElement;
    if (force || sc.scrollHeight - sc.scrollTop - sc.clientHeight < 220) requestAnimationFrame(() => { sc.scrollTop = sc.scrollHeight; });
  }

  // ------------------------------------------------------------------ events
  addEvent(e) {
    if (this.seen.has(e.id)) return;
    this.seen.add(e.id);
    const p = e.payload || {};
    if (e.kind === "trace" || e.kind === "draft_start") return this.addTrace(e);
    if (e.kind !== "message" || e.role !== "user") this.endStream(e);
    let node;
    if (e.kind === "message") node = e.role === "user" ? this.userMsg(e) : this.botMsg(e, p.markdown ? render(p.text, { citations: true }) : null);
    else if (e.kind === "plan") node = this.planCard(e);
    else if (e.kind === "report") { node = this.reportCard(e); this.panel.reload(); this.root.classList.add("panel-open"); }
    else if (e.kind === "file") { node = this.fileCard(e); this.panel.reload(); }
    else if (e.kind === "error") node = this.errorCard(e);
    if (node) { this.timeline.append(node); this.scroll(e.role === "user"); }
    // a plan / report / error ends the visible run (also when replaying history after a reconnect)
    const ends = ["plan", "report", "error", "file"].includes(e.kind) || (e.kind === "message" && e.role === "assistant");
    if (ends && !ACTIVE.has(this.brief.status)) this.closeActivity();
  }

  userMsg(e) {
    return h("div", { class: "msg user" }, h("div", { class: "bubble" }, e.payload.text), h("time", {}, time(e.created_at)));
  }

  botMsg(e, html) {
    const who = AGENTS[e.agent] || AGENTS.chat;
    return h("div", { class: "msg bot" }, h("div", { class: "avatar", title: who.label }, "B"),
      h("div", { class: "bubble" }, html ? h("div", { class: "md", html }) : e.payload.text), h("time", {}, time(e.created_at)));
  }

  // live "agents at work" card: one per run, steps stream into it
  addTrace(e) {
    const p = e.payload || {};
    if (!this.activity || this.activity.dataset.run !== String(e.run_id ?? "")) {
      this.closeActivity();
      this.activityList = h("ol", { class: "steps" });
      this.activityHead = h("summary", {}, h("span", { class: "spinner" }), h("strong", {}, "Agents at work"), h("span", { class: "muted small count" }, ""));
      this.activity = h("details", { class: "activity live", open: true, dataset: { run: String(e.run_id ?? "") } }, this.activityHead, this.activityList);
      this.timeline.append(this.activity);
      this.steps = 0;
    }
    if (e.kind === "draft_start") {
      if (p.target === "answer") this.startStream("chat");
      else this.startStream("writer");
      return;
    }
    const who = AGENTS[e.agent] || { label: e.agent, emoji: "•" };
    let detail = p.detail || p.event;
    const extra = [];
    if (p.event === "plan" && p.plan) extra.push(h("ul", { class: "plan-mini" }, p.plan.map((q) => h("li", {}, q))));
    if (p.event === "route" && p.thought) extra.push(h("div", { class: "thought" }, "💭 ", p.thought));
    if (p.event === "source" && p.url?.startsWith("http")) {
      detail = null;
      extra.push(h("span", {}, `Read [${p.n}] `, h("a", { href: p.url, target: "_blank", rel: "noopener noreferrer" }, p.detail.replace(/^\[\d+\]\s*/, ""))));
    }
    if (p.event === "source" && p.origin?.startsWith("mcp")) extra.push(h("span", { class: "tag mcp" }, "MCP"));
    const cls = ["step", `a-${e.agent}`, p.event === "approve" ? "good" : "", ["research", "rewrite", "gap", "warning"].includes(p.event) ? "warn" : ""].join(" ");
    this.activityList.append(h("li", { class: cls }, h("span", { class: "who" }, `${who.emoji} ${who.label}`),
      h("div", { class: "what" }, detail, ...extra)));
    this.steps++;
    this.activityHead.querySelector(".count").textContent = ` · ${this.steps} steps`;
    this.scroll();
  }

  closeActivity() {
    if (!this.activity) return;
    this.activity.classList.remove("live");
    this.activity.open = false;
    this.activityHead.querySelector(".spinner")?.replaceWith(icon("check", "icon ok"));
    this.activity = null;
  }

  startStream(agent) {
    this.endStream();
    const body = h("div", { class: "md" });
    const label = agent === "writer" ? "✍️ Writer is drafting the report…" : "💬 Answering…";
    this.streaming = { agent, text: "", body, node: h("div", { class: "msg bot streaming" }, h("div", { class: "avatar" }, "B"),
      h("div", { class: "bubble" }, h("div", { class: "muted small" }, label), body)) };
    this.timeline.append(this.streaming.node);
    this.scroll();
  }

  addDelta(d) {
    if (!this.streaming || this.streaming.agent !== d.agent) this.startStream(d.agent);
    this.streaming.text += d.text;
    cancelAnimationFrame(this.raf);
    this.raf = requestAnimationFrame(() => {
      if (this.streaming) this.streaming.body.innerHTML = render(this.streaming.text.slice(-6000), { citations: true });
      this.scroll();
    });
  }

  endStream(e) {
    if (!this.streaming) return;
    // the final message / report card replaces the streamed preview
    if (!e || ["message", "report", "error", "plan", "file"].includes(e.kind)) {
      this.streaming.node.remove();
      this.streaming = null;
    }
  }

  planCard(e) {
    const p = e.payload;
    const awaiting = p.awaiting && this.brief.status === "awaiting_approval";
    const list = h("ol", { class: "plan-list" });
    const addRow = (q) => {
      const input = h("input", { value: q, maxlength: 300, "aria-label": "Sub-question", disabled: !awaiting });
      list.append(h("li", {}, input, awaiting ? h("button", { class: "icon-btn", title: "Remove", "aria-label": "Remove question",
        onclick: (ev) => ev.currentTarget.parentElement.remove(), html: icons.x }) : null));
    };
    (p.plan || []).forEach(addRow);
    const titleIn = h("input", { class: "plan-title", value: p.title || "", maxlength: 200, disabled: !awaiting, "aria-label": "Report title" });
    const actions = awaiting ? h("div", { class: "row" },
      h("button", { class: "btn ghost", onclick: () => list.children.length < 6 ? addRow("") : toast("At most 6 questions") }, icon("plus"), "Add question"),
      h("button", { class: "btn primary", onclick: (ev) => {
        const plan = [...list.querySelectorAll("input")].map((i) => i.value.trim()).filter(Boolean);
        if (!plan.length) return toast("Add at least one question", "error");
        this.socket.send({ type: "approve_plan", plan, title: titleIn.value.trim() || undefined });
        ev.currentTarget.disabled = true;
        card.classList.add("approved");
        card.querySelectorAll("input,button").forEach((x) => { x.disabled = true; });
      } }, icon("check"), "Approve & start research")) : h("p", { class: "muted small" }, p.awaiting ? "Superseded" : "Approved");
    const card = h("div", { class: "card plan" },
      h("div", { class: "card-head" }, h("span", { class: "badge" }, "🧭 Research plan"), p.change ? h("span", { class: "muted small" }, p.change) : null),
      titleIn, list, awaiting ? h("p", { class: "muted small" }, "Edit, add or remove questions - or just tell me what to change.") : null, actions);
    // older plan cards become read-only when a newer one arrives
    this.timeline.querySelectorAll(".card.plan:not(.approved)").forEach((c) => {
      c.classList.add("approved");
      c.querySelectorAll("input,button").forEach((x) => { x.disabled = true; });
    });
    return card;
  }

  reportCard(e) {
    const p = e.payload;
    const v = p.verification || {};
    const c = v.counts || {};
    const summary = (p.markdown.split(/\n## /)[1] || "").replace(/^[^\n]*\n/, "").trim().slice(0, 420);
    const pct = v.ratio != null ? Math.round(v.ratio * 100) : null;
    return h("div", { class: "card report" },
      h("div", { class: "card-head" }, h("span", { class: "badge" }, `📄 Report v${p.version}`),
        pct != null ? h("span", { class: `pill ${pct >= 80 ? "ok" : pct >= 60 ? "wait" : "bad"}`, title: "Citations checked against the text of their sources" },
          icon("shield"), `${pct}% verified`) : null),
      h("h3", {}, p.title),
      p.instruction ? h("p", { class: "muted small" }, "Revision: ", p.instruction) : null,
      h("div", { class: "md clamp", html: render(summary, { citations: true }) }),
      pct != null ? h("p", { class: "muted small" }, `${c.verified || 0} verified · ${c.weak || 0} weak · ${(c.unsupported || 0) + (c.missing_source || 0)} unsupported citations`) : null,
      h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: () => { this.root.classList.add("panel-open"); this.panel.show("report", p.version); } }, icon("doc"), "Open report"),
        ...["PDF", "Word", "Slides", "Infographic"].map((f) => h("button", { class: "btn ghost", onclick: () => this.send(`Give me this as ${f}`) }, f))));
  }

  fileCard(e) {
    const { file, note } = e.payload;
    const preview = h("div", { class: "preview" });
    const card = h("div", { class: "card file" },
      note ? h("p", { class: "note" }, icon("alert"), note) : null,
      h("div", { class: "file-row" }, h("span", { class: "ficon" }, FORMAT_ICON[file.format] || "📄"),
        h("div", { class: "fmeta" }, h("strong", {}, file.filename), h("span", { class: "muted small" }, `${file.description} · ${bytes(file.size)}`)),
        h("button", { class: "btn primary", onclick: () => saveFile(file).catch((err) => toast(err.message, "error")) }, icon("download"), "Download")),
      preview);
    if (file.format === "png" || file.format === "jpg") {
      downloadFile(file, { inline: true }).then((blob) => {
        const url = URL.createObjectURL(blob);
        preview.append(h("img", { src: url, alt: file.filename, loading: "lazy", onclick: () => window.open(url, "_blank", "noopener") }));
        this.scroll();
      }).catch(() => {});
    }
    return card;
  }

  errorCard(e) {
    const p = e.payload;
    return h("div", { class: "card error" }, h("p", {}, icon("alert"), p.message),
      p.retryable ? h("button", { class: "btn primary", onclick: () => this.send("retry") }, icon("refresh"), "Retry") : null);
  }
}
