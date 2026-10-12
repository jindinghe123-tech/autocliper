// App shell: sidebar, routing, theme.
import { del, get } from "./api.js";
import { showChat, stopChatView } from "./chat.js";
import { confirmDialog, h, icon, toast } from "./dom.js";
import { showFiles } from "./files.js";
import { showSettings } from "./settings.js";
import { anyProviderReady, emit, modelLabel, navigate, on, providerName, refreshSessions, refreshSettings, store } from "./store.js";
import { showSubtitles, stopSubtitleView } from "./subtitles.js";
import { showTasks } from "./tasks.js";

const NAV = [
  ["#/chat", "chat", "对话"],
  ["#/subtitles", "captions", "字幕编辑"],
  ["#/tasks", "clock", "定时任务"],
  ["#/files", "folder", "文件"],
  ["#/settings", "settings", "设置"],
];

function applyTheme() {
  const pref = localStorage.getItem("theme") || "dark";
  const dark = pref === "system" ? matchMedia("(prefers-color-scheme: dark)").matches : pref === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function buildSidebar() {
  const sb = document.getElementById("sidebar");
  sb.innerHTML = "";
  const nav = h("nav", { class: "nav" }, NAV.map(([href, ico, label]) => h("a", { href, html: icon(ico) + `<span>${label}</span>`, dataset: { route: href } })));
  const sessions = h("div", { class: "sessions" });
  const chip = h("div", { class: "model-chip", title: "模型设置", onclick: () => navigate("#/settings/models") });
  const themeBtn = h("button", { class: "btn ghost sm icon-only", title: "切换主题" });
  const renderTheme = () => { themeBtn.innerHTML = icon(document.documentElement.dataset.theme === "dark" ? "sun" : "moon"); };
  themeBtn.onclick = () => {
    localStorage.setItem("theme", document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    applyTheme();
    renderTheme();
  };
  renderTheme();
  sb.append(
    h("div", { class: "brand" }, h("div", { class: "brand-logo", html: icon("play") }), "AutoStudio", h("small", {}, `v${store.meta?.version || ""}`)),
    h("button", { class: "btn primary new-chat", html: icon("plus") + "新对话", onclick: () => { navigate("#/chat"); sb.classList.remove("open"); } }),
    nav, h("div", { class: "side-title" }, "最近"), sessions,
    h("div", { class: "side-foot" }, chip, themeBtn));

  const renderChip = () => {
    const a = store.settings?.active || {};
    const ready = anyProviderReady();
    chip.innerHTML = `<span class="status-dot ${ready ? "ok" : "bad"}"></span><span class="t">${ready ? `${providerName(a.provider)} · ${modelLabel(a.provider, a.model)}` : "未连接模型 · 点击设置"}</span>`;
  };
  const renderSessions = () => {
    sessions.innerHTML = "";
    const cur = currentSessionId();
    if (!store.sessions.length) sessions.append(h("div", { class: "small muted", style: { padding: "6px 10px" } }, "还没有对话"));
    for (const s of store.sessions) {
      const item = h("div", { class: `side-item ${s.id === cur ? "active" : ""}`, title: s.title, onclick: () => { navigate(`#/chat/${s.id}`); sb.classList.remove("open"); } },
        s.running ? h("span", { class: "dot-run" }) : h("span", { html: icon(s.kind === "task" ? "clock" : "chat"), style: { color: "var(--muted)", display: "grid" } }),
        h("span", { class: "t" }, s.title),
        h("button", { class: "del", html: icon("trash"), title: "删除", onclick: async (e) => {
          e.stopPropagation();
          if (!(await confirmDialog(`删除对话「${s.title}」？`, { danger: true, okText: "删除" }))) return;
          await del(`/api/sessions/${s.id}`);
          if (currentSessionId() === s.id) navigate("#/chat");
          refreshSessions();
        } }));
      sessions.append(item);
    }
  };
  on("sessions", renderSessions);
  on("settings", renderChip);
  on("theme", () => { applyTheme(); renderTheme(); });
  on("route", () => {
    const hash = location.hash || "#/chat";
    nav.querySelectorAll("a").forEach((a) => a.classList.toggle("active", hash.startsWith(a.dataset.route)));
    renderSessions();
  });
  renderChip();
  renderSessions();
}

function currentSessionId() {
  const m = (location.hash || "").match(/^#\/chat\/([\w-]+)/);
  return m ? m[1] : null;
}

async function route() {
  const main = document.getElementById("main");
  const hash = location.hash || "#/chat";
  const [pathPart, query = ""] = hash.slice(1).split("?");
  const parts = pathPart.split("/").filter(Boolean);
  const params = new URLSearchParams(query);
  stopChatView();
  stopSubtitleView();
  try {
    switch (parts[0]) {
      case "subtitles": await showSubtitles(main, params); break;
      case "tasks": await showTasks(main); break;
      case "files": await showFiles(main, params); break;
      case "settings": await showSettings(main, parts[1] || "models", params); break;
      default: await showChat(main, parts[1] || null);
    }
  } catch (e) {
    console.error(e);
    toast(e.message || String(e), "err");
  }
  emit("route");
}

async function boot() {
  applyTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  try {
    [store.meta] = await Promise.all([get("/api/meta"), refreshSettings(), refreshSessions()]);
  } catch (e) {
    document.getElementById("main").innerHTML = `<div class="empty">无法连接 AutoStudio 服务：${e.message}</div>`;
    return;
  }
  buildSidebar();
  window.addEventListener("hashchange", route);
  await route();
  // keep the "running" dots fresh
  setInterval(() => { if (document.visibilityState === "visible") refreshSessions().catch(() => {}); }, 8000);
  if (!store.meta.ffmpeg?.ffmpeg) toast("未找到 ffmpeg，视频功能不可用。请安装 ffmpeg 或 pip install imageio-ffmpeg。", "err");
}

boot();
