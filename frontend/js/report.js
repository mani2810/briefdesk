// Report panel: the current report with verification badges on every citation, sources, files, versions.
import { api, saveFile } from "./api.js";
import { render } from "./markdown.js";
import { bytes, h, icon, ago, toast } from "./ui.js";

export class ReportPanel {
  constructor(root, briefId, { onCite } = {}) {
    this.root = root;
    this.briefId = briefId;
    this.onCite = onCite;
    this.tab = "report";
    this.version = null;
    this.data = null;
    this.reload();
  }

  destroy() {}

  async reload() {
    try {
      this.data = await api(`/briefs/${this.briefId}`);
      this.versions = this.data.version ? await api(`/briefs/${this.briefId}/versions`) : [];
      this.draw();
    } catch (e) {
      if (e.status !== 404) toast(e.message, "error");
    }
  }

  show(tab, version) {
    this.tab = tab;
    if (version) this.version = version;
    this.draw();
  }

  draw() {
    const d = this.data;
    if (!d) return;
    const tabs = [["report", "Report"], ["sources", `Sources (${d.sources.length})`], ["files", `Files (${d.files.length})`]];
    if (this.versions.length > 1) tabs.push(["versions", `Versions (${this.versions.length})`]);
    const nav = h("nav", { class: "tabs", role: "tablist" }, tabs.map(([k, label]) =>
      h("button", { role: "tab", "aria-selected": String(this.tab === k), class: this.tab === k ? "on" : "", onclick: () => this.show(k) }, label)));
    const body = h("div", { class: "panel-body" });
    if (this.tab === "report") body.append(this.reportView());
    if (this.tab === "sources") body.append(this.sourcesView());
    if (this.tab === "files") body.append(this.filesView());
    if (this.tab === "versions") body.append(this.versionsView());
    this.root.replaceChildren(h("div", { class: "panel-head" }, nav,
      h("button", { class: "icon-btn mobile-only", "aria-label": "Close panel", onclick: () => this.root.parentElement.classList.remove("panel-open"), html: '<span aria-hidden="true">✕</span>' })), body);
  }

  reportView() {
    const d = this.data;
    const v = this.versions.find((x) => x.version === (this.version || d.version));
    const md = v ? v.markdown : d.report_md;
    if (!md) {
      return h("div", { class: "empty" }, h("p", { class: "big" }, "📄"), h("p", {}, "The report will appear here when the agents finish."),
        d.sources.length ? h("p", { class: "muted small" }, `${d.sources.length} sources read so far.`) : null);
    }
    const latest = !v || v.version === d.version;
    const status = {};
    if (latest) for (const c of d.citations) status[c.n] = worst(status[c.n], c.status);
    const art = h("article", { class: "md report-md", html: render(md, { citations: true, citationStatus: (n) => status[n] || "" }) });
    art.addEventListener("click", (ev) => {
      const b = ev.target.closest("button.cite");
      if (b) this.citePopover(b, Number(b.dataset.n), latest);
    });
    const pct = latest && d.verified_ratio != null ? Math.round(d.verified_ratio * 100) : null;
    return h("div", {},
      h("div", { class: "report-bar" },
        h("span", { class: "muted small" }, `Version ${v ? v.version : d.version}${v?.instruction ? " · " + v.instruction : ""}`),
        pct != null ? h("span", { class: "legend small" }, h("i", { class: "cite verified" }, "n"), " verified ", h("i", { class: "cite weak" }, "n"), " weak ",
          h("i", { class: "cite unsupported" }, "n"), " unsupported") : null),
      art);
  }

  citePopover(btn, n, latest) {
    document.querySelector(".popover")?.remove();
    const src = this.data.sources.find((s) => s.n === n);
    const checks = latest ? this.data.citations.filter((c) => c.n === n) : [];
    const sentence = btn.closest("p,li,td")?.textContent.replace(/\s*\d+\s*$/, "").trim().slice(0, 220);
    const check = checks.find((c) => sentence && c.sentence.slice(0, 40) && sentence.includes(c.sentence.slice(0, 30))) || checks[0];
    const pop = h("div", { class: "popover", role: "dialog" },
      h("div", { class: "row between" }, h("strong", {}, `Source [${n}]`),
        check ? h("span", { class: `pill ${check.status === "verified" ? "ok" : check.status === "weak" ? "wait" : "bad"}` }, check.status.replace("_", " ")) : null),
      src ? h("p", {}, src.url.startsWith("http") ? h("a", { href: src.url, target: "_blank", rel: "noopener noreferrer" }, src.title) : src.title,
        h("span", { class: "muted small" }, ` · ${src.domain}`)) : h("p", { class: "bad" }, "This number does not match any source."),
      check?.evidence ? h("blockquote", {}, "“", check.evidence.slice(0, 380), "”") : null,
      h("button", { class: "btn ghost small", onclick: () => pop.remove() }, "Close"));
    const r = btn.getBoundingClientRect();
    document.body.append(pop);
    const pw = pop.offsetWidth;
    pop.style.left = Math.max(8, Math.min(window.innerWidth - pw - 8, r.left - pw / 2)) + "px";
    pop.style.top = Math.min(window.innerHeight - pop.offsetHeight - 8, r.bottom + 8) + "px";
    setTimeout(() => document.addEventListener("click", function off(ev) {
      if (!pop.contains(ev.target)) { pop.remove(); document.removeEventListener("click", off); }
    }), 0);
  }

  sourcesView() {
    const s = this.data.sources;
    if (!s.length) return h("div", { class: "empty" }, h("p", {}, "No sources yet."));
    return h("ol", { class: "sources" }, s.map((x) => h("li", {},
      h("span", { class: "n" }, x.n),
      h("div", {}, x.url.startsWith("http") ? h("a", { href: x.url, target: "_blank", rel: "noopener noreferrer" }, x.title) : h("strong", {}, x.title),
        h("div", { class: "muted small" }, x.origin.startsWith("mcp") ? h("span", { class: "tag mcp" }, "MCP · " + x.origin.slice(4)) : h("span", { class: "tag" }, "web"), " ", x.domain),
        x.snippet ? h("p", { class: "small" }, x.snippet.slice(0, 220), "…") : null))));
  }

  filesView() {
    const f = this.data.files;
    if (!f.length) return h("div", { class: "empty" }, h("p", {}, "No files yet. Ask in the chat: “as PDF”, “Word”, “slides”, “infographic”, “Excel”…"));
    return h("ul", { class: "files" }, f.map((x) => h("li", {},
      h("div", {}, h("strong", {}, x.filename), h("div", { class: "muted small" }, `${x.description} · ${bytes(x.size)} · ${ago(x.created_at)}`)),
      h("button", { class: "btn ghost", "aria-label": `Download ${x.filename}`, onclick: () => saveFile(x).catch((e) => toast(e.message, "error")) }, icon("download")))));
  }

  versionsView() {
    return h("ul", { class: "versions" }, [...this.versions].reverse().map((v) => h("li", {},
      h("div", {}, h("strong", {}, `v${v.version}`), h("span", { class: "muted small" }, ` · ${ago(v.created_at)}`),
        v.verified_ratio != null ? h("span", { class: "muted small" }, ` · ${Math.round(v.verified_ratio * 100)}% verified`) : null,
        h("div", { class: "small" }, v.instruction || "First version")),
      h("button", { class: "btn ghost", onclick: () => this.show("report", v.version) }, "View"))));
  }
}

const RANK = { verified: 0, weak: 1, unsupported: 2, missing_source: 3 };
function worst(a, b) {
  if (!a) return b;
  return (RANK[b] ?? 0) > (RANK[a] ?? 0) ? b : a;
}
