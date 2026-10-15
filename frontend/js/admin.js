// Admin: platform health (LLM gateway + MCP), usage, failed runs, users.
import { api } from "./api.js";
import { ago, h, icons, toast } from "./ui.js";

export class AdminView {
  constructor(root) {
    this.root = root;
    this.load();
    this.timer = setInterval(() => this.load(), 10000);
  }

  destroy() { clearInterval(this.timer); }

  async load() {
    try {
      const [o, runs, users] = await Promise.all([api("/admin/overview"), api("/admin/runs?limit=30"), api("/admin/users")]);
      this.draw(o, runs, users);
    } catch (e) { toast(e.message, "error"); }
  }

  draw(o, runs, users) {
    const llm = o.llm || {};
    const stat = (label, value, sub) => h("div", { class: "stat" }, h("span", { class: "muted small" }, label), h("strong", {}, value ?? "–"), sub ? h("span", { class: "muted small" }, sub) : null);
    const breakers = Object.entries(llm.breakers || {});
    const byKind = Object.entries(llm.by_kind || {}).sort((a, b) => b[1].tokens - a[1].tokens);
    const form = this.userForm();
    this.root.replaceChildren(h("div", { class: "admin" },
      h("header", { class: "admin-head" },
        h("button", { class: "icon-btn mobile-only", "aria-label": "Menu", onclick: () => document.body.classList.toggle("nav-open"), html: icons.menu }),
        h("h1", {}, "Admin"), h("span", { class: "muted small" }, "auto-refreshes every 10 s")),
      h("div", { class: "stats" },
        stat("Users", o.users), stat("Briefs", o.briefs, Object.entries(o.briefs_by_status).map(([k, v]) => `${v} ${k}`).join(" · ")),
        stat("LLM calls (24 h)", o.llm_calls_24h), stat("Tokens (24 h)", o.tokens_24h?.toLocaleString()),
        stat("Cache hits", llm.cache_hits ?? "–", `${llm.waits ?? 0} pacing waits · ${llm.retries ?? 0} retries`),
        stat("LLM", llm.ready ? "Ready" : "Not ready", llm.ready ? `${llm.mode} · ${Object.values(llm.models || {}).join(" / ")}` : llm.problem)),
      h("div", { class: "grid2" },
        h("section", { class: "card" }, h("h3", {}, "LLM gateway"),
          h("p", { class: "small" }, breakers.length ? breakers.map(([m, s]) => h("span", { class: `pill ${s === "closed" ? "ok" : "bad"}` }, `${m}: circuit ${s}`)) : "No calls yet."),
          byKind.length ? h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, h("th", {}, "Agent"), h("th", {}, "Calls"), h("th", {}, "Tokens"))),
            h("tbody", {}, byKind.map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", {}, v.llm_calls), h("td", {}, v.tokens.toLocaleString()))))) : null),
        h("section", { class: "card" }, h("h3", {}, "Research tools"),
          h("ul", { class: "plain" }, o.tools.map((t) => h("li", {}, h("strong", {}, t.name), " ", h("span", { class: `tag ${t.origin.startsWith("mcp") ? "mcp" : ""}` }, t.origin)))),
          h("h4", {}, "MCP servers"),
          o.mcp_servers.length ? h("ul", { class: "plain" }, o.mcp_servers.map((s) => h("li", {}, h("span", { class: `pill ${s.status === "ready" ? "ok" : "bad"}` }, s.status), " ", h("strong", {}, s.name), " ",
            h("span", { class: "muted small" }, s.error || s.tools.join(", "))))) : h("p", { class: "muted small" }, "None configured (see mcp_servers.example.json)."))),
      h("section", { class: "card" }, h("h3", {}, "Recent runs"),
        h("div", { class: "table-wrap" }, h("table", { class: "tbl" },
          h("thead", {}, h("tr", {}, ["Run", "Brief", "Owner", "Kind", "Status", "LLM calls", "Tokens", "When"].map((x) => h("th", {}, x)))),
          h("tbody", {}, runs.map((r) => h("tr", { class: r.status === "failed" ? "bad-row" : "" },
            h("td", {}, r.id), h("td", {}, h("a", { href: `#/brief/${r.brief_id}` }, r.brief)), h("td", {}, r.owner), h("td", {}, r.kind),
            h("td", { title: r.error || "" }, r.status, r.error ? ` – ${r.error}` : ""), h("td", {}, r.llm_calls), h("td", {}, r.tokens.toLocaleString()), h("td", {}, ago(r.created_at)))))))),
      h("section", { class: "card" }, h("h3", {}, "Users"), form,
        h("div", { class: "table-wrap" }, h("table", { class: "tbl" },
          h("thead", {}, h("tr", {}, ["Name", "Email", "Role", "Active", "Joined", ""].map((x) => h("th", {}, x)))),
          h("tbody", {}, users.map((u) => h("tr", {},
            h("td", {}, u.name), h("td", {}, u.email), h("td", {}, u.role), h("td", {}, u.is_active ? "yes" : "no"), h("td", {}, ago(u.created_at)),
            h("td", {}, h("button", { class: "btn ghost small", onclick: () => this.patch(u.id, { is_active: !u.is_active }) }, u.is_active ? "Disable" : "Enable"))))))))));
  }

  userForm() {
    const name = h("input", { placeholder: "Name", required: true, maxlength: 100 });
    const email = h("input", { type: "email", placeholder: "Email", required: true });
    const pw = h("input", { type: "password", placeholder: "Initial password", required: true, autocomplete: "new-password" });
    const role = h("select", {}, h("option", { value: "user" }, "user"), h("option", { value: "admin" }, "admin"));
    return h("form", { class: "row wrap", onsubmit: async (e) => {
      e.preventDefault();
      try {
        await api("/admin/users", { method: "POST", body: { name: name.value, email: email.value, password: pw.value, role: role.value } });
        toast("User created", "ok");
        this.load();
      } catch (ex) { toast(ex.message, "error"); }
    } }, name, email, pw, role, h("button", { class: "btn primary", type: "submit" }, "Add user"));
  }

  async patch(id, body) {
    try {
      await api(`/admin/users/${id}`, { method: "PATCH", body });
      this.load();
    } catch (e) { toast(e.message, "error"); }
  }
}
