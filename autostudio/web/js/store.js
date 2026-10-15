// Shared app state + tiny event bus.
import { get, put } from "./api.js";

export const store = {
  meta: null,
  settings: null,
  chatgpt: null,
  sessions: [],
  composerDraft: null, // {text, attachments} handed to the chat view by other pages
};

const listeners = {};
export function on(evt, fn) { (listeners[evt] ||= new Set()).add(fn); return () => listeners[evt].delete(fn); }
export function emit(evt, data) { (listeners[evt] || []).forEach((fn) => fn(data)); }

export async function refreshSessions() {
  store.sessions = await get("/api/sessions");
  emit("sessions");
}

export async function refreshSettings() {
  const [settings, chatgpt] = await Promise.all([get("/api/settings"), get("/api/auth/chatgpt")]);
  store.settings = settings;
  store.chatgpt = chatgpt;
  emit("settings");
}

export function providerReady(pid, p) {
  if (!p) return false;
  if (p.type === "chatgpt") return !!store.chatgpt?.logged_in;
  if (p.api_key_set) return true;
  return /localhost|127\.0\.0\.1/.test(p.base_url || "");
}

export function modelOptions() {
  const out = [];
  const provs = store.settings?.providers || {};
  for (const [pid, p] of Object.entries(provs)) {
    const ready = providerReady(pid, p);
    for (const m of p.models || []) {
      if (!m) continue;
      const label = (p.catalog || []).find((c) => c.slug === m)?.display_name || m;
      out.push({ pid, name: p.name || pid, model: m, label, ready, type: p.type });
    }
  }
  return out;
}

export function anyProviderReady() {
  return Object.entries(store.settings?.providers || {}).some(([pid, p]) => providerReady(pid, p));
}

export async function setActive(pid, model) {
  store.settings = await put("/api/settings", { active: { provider: pid, model } });
  emit("settings");
}

export function modelLabel(pid, model) {
  return (store.settings?.providers?.[pid]?.catalog || []).find((c) => c.slug === model)?.display_name || model || "";
}

export function providerName(pid) {
  return store.settings?.providers?.[pid]?.name || pid || "";
}

// Opened from a paired phone (through the phone listener) rather than on the PC itself.
export const isRemote = () => !!store.meta?.remote;
// Running inside the AutoStudio Android app (its WebView adds this to the user agent).
export const inPhoneApp = () => /AutoStudioApp\//.test(navigator.userAgent);

export function navigate(hash) {
  if (location.hash === hash) window.dispatchEvent(new HashChangeEvent("hashchange"));
  else location.hash = hash;
}
