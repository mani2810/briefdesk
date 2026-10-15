// BriefDesk single-page app: auth, sidebar of briefs, routing (#/new, #/brief/12, #/admin).
import { api, auth, onAuthChange } from "./api.js";
import { ChatView } from "./chat.js";
import { AdminView } from "./admin.js";
import { openSettings } from "./settings.js";
import { ago, confirmDialog, h, icon, icons, statusPill, toast } from "./ui.js";

const app = document.getElementById("app");
const state = { user: null, meta: null, briefs: [], view: null };

const EXAMPLES = [
  "Quick-commerce in India: market size, key players and risks",
  "UPI adoption by small merchants in India",
  "Electric two-wheelers in India: demand drivers and outlook",
  "How are banks using generative AI in customer service?",
];

// ------------------------------------------------------------------ theme
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("bd-theme", t); } catch { /* private mode */ }
}
let savedTheme = null;
try { savedTheme = localStorage.getItem("bd-theme"); } catch { /* private mode */ }
applyTheme(savedTheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));

// ------------------------------------------------------------------ boot
onAuthChange((user) => {
  const changed = (user?.id || null) !== (state.user?.id || null);
  state.user = user;
  if (changed) route();
});

async function boot() {
  state.meta = await api("/meta", { auth: false }).catch(() => ({}));
  await auth.restore();
  if (!state.user) route();
  window.addEventListener("hashchange", route);
}

function route() {
  state.view?.destroy?.();
  state.view = null;
  if (!state.user) return authScreen();
  const hash = location.hash || "#/new";
  shell();
  const m = hash.match(/^#\/brief\/(\d+)/);
  if (m) return openBrief(Number(m[1]));
  if (hash === "#/admin" && state.user.role === "admin") {
    state.view = new AdminView(state.main);
    return;
  }
  newBriefScreen();
}

// ------------------------------------------------------------------ auth
function authScreen() {
  let mode = "login";
  const err = h("p", { class: "form-error", role: "alert" });
  const name = h("input", { name: "name", autocomplete: "name", placeholder: "Your name", maxlength: 100 });
  const email = h("input", { name: "email", type: "email", autocomplete: "email", placeholder: "you@company.com", required: true });
  const pw = h("input", { name: "password", type: "password", autocomplete: "current-password", placeholder: "Password", required: true });
  const nameRow = h("label", { class: "field hidden" }, "Name", name);
  const submit = h("button", { class: "btn primary block", type: "submit" }, "Log in");
  const toggle = h("button", { type: "button", class: "link" }, "Create an account");
  const form = h("form", { class: "auth-form", onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    submit.disabled = true;
    try {
      if (mode === "login") await auth.login(email.value, pw.value);
      else await auth.register(name.value, email.value, pw.value);
      location.hash = "#/new";
    } catch (ex) {
      err.textContent = ex.message;
    } finally { submit.disabled = false; }
  } }, nameRow, h("label", { class: "field" }, "Email", email), h("label", { class: "field" }, "Password", pw), err, submit,
    state.meta?.allow_registration !== false ? h("p", { class: "muted small center" }, toggle) : null);
  toggle.addEventListener("click", () => {
    mode = mode === "login" ? "register" : "login";
    nameRow.classList.toggle("hidden", mode === "login");
    name.required = mode === "register";
    pw.autocomplete = mode === "login" ? "current-password" : "new-password";
    pw.placeholder = mode === "login" ? "Password" : "At least 10 characters, letters and digits";
    submit.textContent = mode === "login" ? "Log in" : "Create account";
    toggle.textContent = mode === "login" ? "Create an account" : "I already have an account";
  });
  app.replaceChildren(h("main", { class: "auth" },
    h("section", { class: "auth-hero" },
      h("div", { class: "logo big" }, h("span", { class: "mark" }, "B"), "BriefDesk"),
      h("h1", {}, "Research briefs you can ", h("em", {}, "trust"), "."),
      h("p", { class: "lead" }, "A team of AI agents plans the research, reads the web and your own documents, writes a cited brief, reviews it and checks every citation against its source - while you chat with them live."),
      h("ul", { class: "ticks" },
        h("li", {}, "🧭 You approve the plan before research starts"),
        h("li", {}, "✅ Every citation verified against the page it came from"),
        h("li", {}, "💬 Steer the agents mid-run, ask questions, request revisions"),
        h("li", {}, "📦 Get it as PDF, Word, slides, an infographic or Excel - just ask"),
        h("li", {}, "🔌 MCP: plug in your own tools, or call BriefDesk from Claude"))),
    h("section", { class: "auth-card" }, h("h2", {}, "Welcome"), form,
      h("p", { class: "muted small demo" }, "Demo: demo@briefdesk.dev / Research2026"),
      state.meta && !state.meta.llm_ready ? h("p", { class: "warn-box small" }, "⚠️ AI model not configured: ", state.meta.llm_problem || "") : null)));
  email.focus();
}

// ------------------------------------------------------------------ shell
function shell() {
  if (state.main && app.contains(state.main)) return;
  state.list = h("nav", { class: "brief-list", "aria-label": "Your briefs" });
  state.search = h("input", { type: "search", placeholder: "Search briefs", "aria-label": "Search briefs", oninput: drawList });
  const u = state.user;
  const side = h("aside", { class: "sidebar" },
    h("div", { class: "side-top" },
      h("a", { class: "logo", href: "#/new" }, h("span", { class: "mark" }, "B"), "BriefDesk"),
      h("button", { class: "icon-btn", title: "Toggle theme", "aria-label": "Toggle theme",
        onclick: () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"), html: icons.moon })),
    h("a", { class: "btn primary block", href: "#/new", onclick: () => document.body.classList.remove("nav-open") }, icon("plus"), "New brief"),
    h("div", { class: "search" }, icon("search"), state.search),
    state.list,
    h("div", { class: "side-foot" },
      u.role === "admin" ? h("a", { class: "nav-link", href: "#/admin" }, icon("shield"), "Admin") : null,
      h("button", { class: "nav-link", onclick: () => openSettings(state.user, state.meta) }, icon("settings"), "Settings & API"),
      h("div", { class: "me" }, h("span", { class: "avatar" }, (u.name || "?")[0].toUpperCase()),
        h("div", {}, h("strong", {}, u.name), h("div", { class: "muted small" }, u.email)),
        h("button", { class: "icon-btn", title: "Log out", "aria-label": "Log out", onclick: () => auth.logout(), html: icons.logout }))));
  state.main = h("main", { class: "main" });
  app.replaceChildren(h("div", { class: "layout" }, side, state.main,
    h("div", { class: "scrim", onclick: () => document.body.classList.remove("nav-open") })));
  loadBriefs();
  clearInterval(state.poll);
  state.poll = setInterval(loadBriefs, 15000);
}

async function loadBriefs() {
  try {
    state.briefs = await api("/briefs");
    drawList();
  } catch { /* offline: keep the old list */ }
}

function drawList() {
  const q = state.search.value.trim().toLowerCase();
  const current = Number((location.hash.match(/brief\/(\d+)/) || [])[1]);
  const items = state.briefs.filter((b) => !q || (b.title + " " + (b.topic || "")).toLowerCase().includes(q));
  state.list.replaceChildren(...(items.length ? items.map((b) => h("div", { class: `brief-item ${b.id === current ? "on" : ""}` },
    h("a", { href: `#/brief/${b.id}`, onclick: () => document.body.classList.remove("nav-open") },
      h("span", { class: "t" }, b.title), h("span", { class: "row small" }, statusPill(b.status), h("span", { class: "muted" }, ago(b.updated_at)))),
    h("button", { class: "icon-btn del", title: "Delete brief", "aria-label": `Delete ${b.title}`, onclick: async () => {
      if (!(await confirmDialog(`Delete “${b.title}” with its report, sources and files?`))) return;
      await api(`/briefs/${b.id}`, { method: "DELETE" }).catch((e) => toast(e.message, "error"));
      if (b.id === current) location.hash = "#/new";
      loadBriefs();
    }, html: icons.trash })))
    : [h("p", { class: "muted small pad" }, q ? "No matches." : "No briefs yet.")]));
}

// ------------------------------------------------------------------ new brief
function newBriefScreen() {
  const topic = h("textarea", { rows: 3, maxlength: 1000, placeholder: "What should the team research? e.g. “Quick-commerce in India: market size, players and risks”", "aria-label": "Research topic" });
  const audience = h("input", { value: "business readers", maxlength: 120, "aria-label": "Audience" });
  const depth = h("select", { "aria-label": "Depth" }, ["quick", "standard", "deep"].map((d) => h("option", { value: d, selected: d === "standard" }, d[0].toUpperCase() + d.slice(1))));
  const approve = h("input", { type: "checkbox", checked: true, "aria-label": "Review the plan first" });
  const go = h("button", { class: "btn primary", type: "submit" }, icon("sparkle"), "Start research");
  const submit = async (e) => {
    e?.preventDefault();
    if (!topic.value.trim()) return topic.focus();
    go.disabled = true;
    try {
      const b = await api("/briefs", { method: "POST", body: { topic: topic.value.trim(), audience: audience.value.trim() || "business readers",
        depth: depth.value, auto_approve: !approve.checked } });
      location.hash = `#/brief/${b.id}`;
      loadBriefs();
    } catch (ex) { toast(ex.message, "error"); go.disabled = false; }
  };
  topic.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) submit(e); });
  const tools = state.meta?.mcp_tools || [];
  state.main.replaceChildren(h("div", { class: "new-brief" },
    h("button", { class: "icon-btn mobile-only menu-fab", "aria-label": "Menu", onclick: () => document.body.classList.toggle("nav-open"), html: icons.menu }),
    h("div", { class: "hello" }, h("h1", {}, `Hi ${state.user.name.split(" ")[0]}, what should we research?`),
      h("p", { class: "muted" }, "The Supervisor plans it, the Researcher reads the web", tools.length ? " and your MCP tools" : "",
        ", the Writer drafts, the Reviewer critiques and the Verifier checks every citation.")),
    h("form", { class: "card new-form", onsubmit: submit }, topic,
      h("div", { class: "opts" },
        h("label", {}, "Audience", audience), h("label", {}, "Depth", depth),
        h("label", { class: "check" }, approve, "Let me approve the plan first"), go)),
    h("div", { class: "examples" }, h("p", { class: "muted small" }, "Try one:"),
      EXAMPLES.map((t) => h("button", { class: "chip", onclick: () => { topic.value = t; topic.focus(); } }, t))),
    tools.length ? h("p", { class: "muted small" }, "🔌 MCP tools available to the researcher: ", tools.join(", ")) : null,
    state.meta && !state.meta.llm_ready ? h("p", { class: "warn-box" }, "⚠️ The AI model is not configured: ", state.meta.llm_problem || "", ". Add GROQ_API_KEY to backend/.env (or set LLM_MODE=fake for an offline demo) and restart.") : null));
  topic.focus();
}

// ------------------------------------------------------------------ brief
async function openBrief(id) {
  drawList();
  let brief;
  try {
    brief = await api(`/briefs/${id}`);
  } catch (e) {
    state.main.replaceChildren(h("div", { class: "empty pad" }, h("p", {}, e.status === 404 ? "Brief not found." : e.message), h("a", { href: "#/new" }, "Start a new brief")));
    return;
  }
  const root = h("div", { class: "brief" });
  state.main.replaceChildren(root);
  state.view = new ChatView(root, brief, {
    onBriefChange: (b) => {
      const item = state.briefs.find((x) => x.id === b.id);
      if (item && (item.status !== b.status || item.title !== b.title)) {
        Object.assign(item, { status: b.status, title: b.title });
        drawList();
      }
      document.title = `${b.title} · BriefDesk`;
    },
  });
  if (brief.report_md && window.innerWidth > 1200) root.classList.add("panel-open");
}

boot();
