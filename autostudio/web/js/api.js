// HTTP helpers. Every mutating request carries X-AutoStudio so the server can reject cross-site requests.

export async function api(path, { method = "GET", body, raw = false } = {}) {
  const opts = { method, headers: { "X-AutoStudio": "1" } };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (raw) return res;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    if (res.status === 401 && data?.code === "unpaired") location.href = "/pair"; // this phone was disconnected on the PC
    const msg = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

export const get = (p) => api(p);
export const post = (p, body = {}) => api(p, { method: "POST", body });
export const put = (p, body = {}) => api(p, { method: "PUT", body });
export const patch = (p, body = {}) => api(p, { method: "PATCH", body });
export const del = (p) => api(p, { method: "DELETE" });

export function rawUrl(path) {
  return `/api/files/raw?path=${encodeURIComponent(path)}`;
}

export function downloadUrl(path, name = "") {
  return `${rawUrl(path)}&download=1${name ? `&name=${encodeURIComponent(name)}` : ""}`;
}

export function thumbUrl(path, w = 360) {
  return `/api/thumb?w=${w}&path=${encodeURIComponent(path)}`;
}

export async function upload(files) {
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  const r = await api("/api/upload", { method: "POST", body: fd });
  return r.files;
}

export function stream(url, onEvent, onEnd) {
  const es = new EventSource(url);
  let closed = false;
  const close = () => { if (!closed) { closed = true; es.close(); onEnd && onEnd(); } };
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.type === "ping") return;
    onEvent(ev);
    if (ev.type === "done" || ev.type === "idle") close();
  };
  es.onerror = () => close();
  return { close };
}
