// API client. The access token lives in memory only (never localStorage: XSS could steal it);
// the refresh token is an httpOnly cookie the browser sends to /api/v1/auth automatically.

const BASE = "/api/v1";
let accessToken = null;
let refreshing = null;
const listeners = new Set();

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : friendly(detail, status));
    this.status = status;
    this.detail = detail;
  }
}

function friendly(detail, status) {
  if (Array.isArray(detail)) {
    return detail.map((d) => `${(d.loc || []).slice(-1)[0] || "field"}: ${d.msg}`.replace("Value error, ", "")).join("; ");
  }
  if (status === 429) return "Too many requests - please wait a moment.";
  return status >= 500 ? "The server had a problem. Please try again." : "Request failed.";
}

export const onAuthChange = (fn) => listeners.add(fn);
const emit = (user) => listeners.forEach((fn) => fn(user));

function setSession(data) {
  accessToken = data?.access_token || null;
  emit(data?.user || null);
  return data?.user || null;
}

async function raw(path, { method = "GET", body, headers = {}, auth = true, form } = {}) {
  const h = { "X-BriefDesk": "1", ...headers };
  if (auth && accessToken) h.Authorization = `Bearer ${accessToken}`;
  let payload;
  if (body !== undefined) {
    h["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  } else if (form) payload = form;
  return fetch(BASE + path, { method, headers: h, body: payload, credentials: "same-origin" });
}

async function parse(res) {
  if (res.status === 204) return null;
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json().catch(() => null) : await res.text();
  if (!res.ok) throw new ApiError(res.status, data?.detail ?? data?.error ?? data);
  return data;
}

export async function refresh() {
  // single-flight: many requests failing at once trigger ONE refresh
  if (!refreshing) {
    refreshing = raw("/auth/refresh", { method: "POST", auth: false })
      .then(async (res) => (res.ok ? setSession(await res.json()) : setSession(null)))
      .catch(() => setSession(null))
      .finally(() => { refreshing = null; });
  }
  return refreshing;
}

export async function api(path, opts = {}) {
  let res = await raw(path, opts);
  if (res.status === 401 && opts.auth !== false) {
    const user = await refresh();
    if (!user) throw new ApiError(401, "Your session ended. Please log in again.");
    res = await raw(path, opts);
  }
  return parse(res);
}

export const auth = {
  async login(email, password) {
    return setSession(await parse(await raw("/auth/login", { method: "POST", body: { email, password }, auth: false })));
  },
  async register(name, email, password) {
    return setSession(await parse(await raw("/auth/register", { method: "POST", body: { name, email, password }, auth: false })));
  },
  async logout() {
    await raw("/auth/logout", { method: "POST" }).catch(() => {});
    setSession(null);
  },
  restore: refresh,
  token: () => accessToken,
};

export async function downloadFile(file, { inline = false } = {}) {
  let res = await raw(`/files/${file.id}${inline ? "?inline=1" : ""}`);
  if (res.status === 401 && (await refresh())) res = await raw(`/files/${file.id}${inline ? "?inline=1" : ""}`);
  if (!res.ok) throw new ApiError(res.status, "Download failed");
  return res.blob();
}

export async function saveFile(file) {
  const blob = await downloadFile(file);
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: file.filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
