// Settings: change password, personal API tokens + ready-to-paste MCP config for Claude Desktop / Code.
import { api, auth } from "./api.js";
import { ago, confirmDialog, h, icon, modal, toast } from "./ui.js";

export function openSettings(user, meta) {
  const tokens = h("ul", { class: "tokens" });
  const created = h("div");
  const name = h("input", { placeholder: "Token name, e.g. Claude Desktop", maxlength: 60, "aria-label": "Token name" });

  async function load() {
    const list = await api("/tokens");
    tokens.replaceChildren(...(list.length ? list.map((t) => h("li", {},
      h("div", {}, h("strong", {}, t.name), h("div", { class: "muted small" }, `${t.prefix}… · created ${ago(t.created_at)} · ${t.last_used_at ? "used " + ago(t.last_used_at) : "never used"}`)),
      h("button", { class: "btn ghost danger-text", onclick: async () => {
        if (!(await confirmDialog(`Revoke “${t.name}”? Apps using it stop working.`, "Revoke"))) return;
        await api(`/tokens/${t.id}`, { method: "DELETE" });
        load();
      } }, "Revoke"))) : [h("li", { class: "muted small" }, "No tokens yet.")]));
  }

  const createForm = h("form", { class: "row", onsubmit: async (e) => {
    e.preventDefault();
    if (!name.value.trim()) return name.focus();
    try {
      const t = await api("/tokens", { method: "POST", body: { name: name.value.trim() } });
      name.value = "";
      const cfg = JSON.stringify({ mcpServers: { briefdesk: { command: "<path-to-repo>\\.venv\\Scripts\\python.exe",
        args: ["<path-to-repo>\\mcp_server\\briefdesk_mcp.py"], env: { BRIEFDESK_URL: location.origin, BRIEFDESK_TOKEN: t.token } } } }, null, 2);
      const copy = (text) => navigator.clipboard.writeText(text).then(() => toast("Copied"), () => toast("Copy failed", "error"));
      created.replaceChildren(h("div", { class: "card token-once" },
        h("p", {}, h("strong", {}, "Copy your token now - it won't be shown again.")),
        h("div", { class: "row" }, h("code", { class: "token" }, t.token), h("button", { class: "btn ghost", type: "button", onclick: () => copy(t.token) }, icon("copy"), "Copy")),
        h("p", { class: "small" }, "Claude Desktop → Settings → Developer → Edit config, add:"),
        h("pre", { class: "code" }, cfg),
        h("button", { class: "btn ghost", type: "button", onclick: () => copy(cfg) }, icon("copy"), "Copy MCP config")));
      load();
    } catch (ex) { toast(ex.message, "error"); }
  } }, name, h("button", { class: "btn primary", type: "submit" }, "Create token"));

  const cur = h("input", { type: "password", autocomplete: "current-password", placeholder: "Current password", "aria-label": "Current password" });
  const next = h("input", { type: "password", autocomplete: "new-password", placeholder: "New password (10+ chars, letters and digits)", "aria-label": "New password" });
  const pwForm = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    try {
      await api("/auth/password", { method: "POST", body: { current_password: cur.value, password: next.value } });
      toast("Password changed - please log in again", "ok");
      auth.logout();
    } catch (ex) { toast(ex.message, "error"); }
  } }, cur, next, h("button", { class: "btn primary", type: "submit" }, "Change password"));

  const body = h("div", { class: "settings" },
    h("section", {}, h("h3", {}, "Account"), h("p", {}, `${user.name} · ${user.email} · ${user.role}`), pwForm),
    h("section", {}, h("h3", {}, "API tokens & MCP"),
      h("p", { class: "muted small" }, "Let Claude Desktop, Claude Code or any MCP client use BriefDesk: create a token, then add the BriefDesk MCP server to the client. Tokens act as you for briefs and files, but can't change your password or tokens."),
      createForm, created, tokens),
    meta?.mcp_tools?.length ? h("section", {}, h("h3", {}, "MCP tools the researcher can use"), h("p", { class: "small" }, meta.mcp_tools.join(", "))) : null);
  modal("Settings", body, { wide: true });
  load().catch((e) => toast(e.message, "error"));
}
