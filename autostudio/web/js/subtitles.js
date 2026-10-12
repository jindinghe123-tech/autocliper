// Subtitle editor: video preview with live overlay + editable cue list.
import { get, put, rawUrl } from "./api.js";
import { autosize, basename, confirmDialog, fmtClock, h, icon, modal, parseClock, toast } from "./dom.js";
import { navigate, store } from "./store.js";

let cleanup = null;

export function stopSubtitleView() {
  if (cleanup) cleanup();
  cleanup = null;
}

async function pickFile(kinds, title) {
  return new Promise((resolve) => {
    const list = h("div", { style: { display: "flex", flexDirection: "column", gap: "4px" } }, h("div", { class: "spinner" }));
    const m = modal({ title, body: list, wide: false, onClose: () => resolve(null) });
    const load = async (path) => {
      const res = await get(`/api/files?path=${encodeURIComponent(path || "")}`);
      list.innerHTML = "";
      if (res.parent) list.append(h("div", { class: "side-item", onclick: () => load(res.parent), html: icon("up") + "<span class='t'>上一级</span>" }));
      for (const it of res.items) {
        if (!it.is_dir && !kinds.includes(it.kind)) continue;
        list.append(h("div", {
          class: "side-item", onclick: () => { if (it.is_dir) load(it.path); else { m.close(); resolve(it.path); } },
          html: icon(it.is_dir ? "folder" : it.kind === "subtitle" ? "captions" : it.kind === "audio" ? "music" : "film") + `<span class="t"></span>`,
        }));
        list.lastChild.querySelector(".t").textContent = it.name;
      }
      if (!list.children.length) list.append(h("div", { class: "empty" }, "这里没有可选的文件"));
    };
    load("");
  });
}

export async function showSubtitles(main, params) {
  stopSubtitleView();
  main.innerHTML = "";
  const path = params.get("path");
  const head = h("div", { class: "page-head" },
    h("button", { class: "btn ghost icon-only menu-btn", html: icon("menu"), onclick: () => document.getElementById("sidebar").classList.toggle("open") }),
    h("h1", {}, "字幕编辑器"));
  main.append(head);
  if (!path) {
    main.append(h("div", { class: "page-body" }, h("div", { class: "page-narrow empty" },
      h("div", { html: icon("captions"), style: { fontSize: "0" } }),
      h("h2", { style: { color: "var(--text)" } }, "打开一个字幕文件"),
      h("p", {}, "让 Agent 用「语音识别」生成字幕后，点击结果里的「字幕编辑器」按钮即可在这里校对；也可以直接打开工作区里的 .srt / .vtt / .ass 文件。"),
      h("button", { class: "btn primary", html: icon("folder") + "选择字幕文件", onclick: async () => {
        const p = await pickFile(["subtitle"], "选择字幕文件");
        if (p) navigate(`#/subtitles?path=${encodeURIComponent(p)}`);
      } }))));
    return;
  }

  let data;
  try { data = await get(`/api/subtitles?path=${encodeURIComponent(path)}`); } catch (e) { toast(e.message, "err"); return; }
  let cues = data.cues.map((c) => ({ start: c.start, end: c.end, text: c.text }));
  let mediaPath = params.get("media") || data.media;
  let dirty = false;
  let active = -1;

  const dirtyDot = h("span", { class: "dirty-dot", style: { display: "none" }, title: "有未保存的修改" });
  const setDirty = (v) => { dirty = v; dirtyDot.style.display = v ? "inline-block" : "none"; };
  head.append(h("span", { class: "tag mono", title: path }, basename(path)), dirtyDot, h("div", { class: "grow" }),
    h("button", { class: "btn sm", html: icon("refresh") + "重新加载", onclick: () => reload() }),
    h("button", { class: "btn sm", html: icon("sparkles") + "让 AI 校对", onclick: () => askAI() }),
    h("button", { class: "btn sm primary", html: icon("save") + "保存", onclick: () => save() }));

  // ---- player
  const overlay = h("div", { class: "sub-overlay" });
  const playerBox = h("div", { class: "player-box" });
  let player = null;
  const mountPlayer = () => {
    playerBox.innerHTML = "";
    if (!mediaPath) {
      playerBox.append(h("div", { class: "empty" }, "未找到对应的视频/音频", h("div", { style: { marginTop: "10px" } },
        h("button", { class: "btn sm", onclick: async () => { const p = await pickFile(["video", "audio"], "选择视频/音频"); if (p) { mediaPath = p; mountPlayer(); } } }, "选择媒体文件"))));
      player = null;
      return;
    }
    const isAudio = /\.(mp3|wav|m4a|aac|flac|ogg|opus)$/i.test(mediaPath);
    player = h(isAudio ? "audio" : "video", { src: rawUrl(mediaPath), controls: true, preload: "metadata" });
    playerBox.append(player, overlay);
    player.addEventListener("timeupdate", onTime);
    player.addEventListener("seeked", onTime);
  };

  // ---- tools
  const search = h("input", { class: "input", placeholder: "查找", style: { flex: 1 } });
  const replace = h("input", { class: "input", placeholder: "替换为", style: { flex: 1 } });
  const offset = h("input", { class: "input", placeholder: "±秒", style: { width: "80px" } });
  const tools = h("div", { class: "card", style: { padding: "14px" } },
    h("div", { class: "small muted", style: { marginBottom: "8px" } }, "批量操作"),
    h("div", { class: "row", style: { marginBottom: "8px" } }, search, replace,
      h("button", { class: "btn sm", onclick: () => doReplace() }, "全部替换")),
    h("div", { class: "row" }, h("span", { class: "small muted" }, "整体平移"), offset,
      h("button", { class: "btn sm", onclick: () => doShift() }, "应用")),
    h("div", { class: "small muted", style: { marginTop: "10px" } },
      h("kbd", {}, "Ctrl+S"), " 保存 · 点击编号跳转播放 · ", h("kbd", {}, "Alt+↑/↓"), " 切换字幕"));
  const stats = h("div", { class: "small muted" });

  const list = h("div", { class: "sub-list" });
  main.append(h("div", { class: "sub-layout" },
    h("div", { class: "sub-player" }, playerBox, stats, tools),
    list));
  mountPlayer();

  function renderStats() {
    const dur = cues.length ? cues[cues.length - 1].end : 0;
    const long = cues.filter((c) => c.text.replace(/\s/g, "").length > 24).length;
    stats.textContent = `共 ${cues.length} 条 · 时长 ${fmtClock(dur).slice(0, 8)}${long ? ` · ${long} 条超过 24 字` : ""}`;
  }

  function rowFor(c, i) {
    const ta = h("textarea", { rows: 1 });
    ta.value = c.text;
    ta.oninput = () => { c.text = ta.value; autosize(ta, 200); setDirty(true); if (i === active) showOverlay(); };
    ta.onfocus = () => { if (player && (player.currentTime < c.start || player.currentTime > c.end)) player.currentTime = c.start + 0.01; };
    const tIn = h("input", { value: fmtClock(c.start) });
    const tOut = h("input", { value: fmtClock(c.end) });
    const commitTime = (inp, key) => {
      const v = parseClock(inp.value);
      if (isNaN(v)) { inp.value = fmtClock(c[key]); return toast("时间格式应为 HH:MM:SS.mmm", "err"); }
      c[key] = v; inp.value = fmtClock(v); setDirty(true);
    };
    tIn.onchange = () => commitTime(tIn, "start");
    tOut.onchange = () => commitTime(tOut, "end");
    const row = h("div", { class: "cue" + (c.text.replace(/\s/g, "").length > 24 ? " flag" : "") },
      h("div", { class: "idx", title: "跳转播放", onclick: () => { if (player) { player.currentTime = c.start + 0.01; player.play(); } } }, i + 1),
      h("div", { class: "times" }, tIn, tOut),
      ta,
      h("div", { class: "ops" },
        h("button", { title: "在下方插入", html: icon("insert"), onclick: () => insertAfter(i) }),
        h("button", { title: "与下一条合并", html: icon("merge"), onclick: () => mergeNext(i) }),
        h("button", { title: "删除", html: icon("trash"), onclick: () => { cues.splice(i, 1); setDirty(true); renderList(); } })));
    requestAnimationFrame(() => autosize(ta, 200));
    return row;
  }

  function renderList() {
    list.innerHTML = "";
    cues.forEach((c, i) => list.append(rowFor(c, i)));
    if (!cues.length) list.append(h("div", { class: "empty" }, "没有字幕", h("div", {}, h("button", { class: "btn sm", onclick: () => insertAfter(-1) }, "添加一条"))));
    renderStats();
  }

  function insertAfter(i) {
    const prev = cues[i];
    const next = cues[i + 1];
    const start = prev ? prev.end + 0.05 : 0;
    const end = next ? Math.max(start + 0.5, next.start - 0.05) : start + 2;
    cues.splice(i + 1, 0, { start, end, text: "" });
    setDirty(true);
    renderList();
    list.children[i + 1]?.querySelector("textarea")?.focus();
  }

  function mergeNext(i) {
    if (i + 1 >= cues.length) return;
    const a = cues[i], b = cues[i + 1];
    const cjk = /[一-鿿]/.test(a.text + b.text);
    cues.splice(i, 2, { start: a.start, end: b.end, text: a.text + (cjk ? "" : " ") + b.text });
    setDirty(true);
    renderList();
  }

  function showOverlay() {
    const c = cues[active];
    overlay.innerHTML = "";
    if (c && c.text.trim()) overlay.append(h("span", {}, c.text));
  }

  function onTime() {
    const t = player.currentTime;
    let idx = cues.findIndex((c) => t >= c.start && t <= c.end);
    if (idx !== active) {
      list.children[active]?.classList.remove("active");
      active = idx;
      const row = list.children[active];
      if (row) {
        row.classList.add("active");
        const r = row.getBoundingClientRect(), lr = list.getBoundingClientRect();
        if (document.activeElement?.tagName !== "TEXTAREA" && (r.top < lr.top || r.bottom > lr.bottom)) row.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      showOverlay();
    }
  }

  function doReplace() {
    const f = search.value;
    if (!f) return;
    let n = 0;
    for (const c of cues) { const parts = c.text.split(f); n += parts.length - 1; c.text = parts.join(replace.value); }
    if (n) { setDirty(true); renderList(); }
    toast(`替换了 ${n} 处`, n ? "ok" : "");
  }

  function doShift() {
    const d = parseFloat(offset.value);
    if (isNaN(d)) return toast("请输入秒数，如 0.5 或 -1.2", "err");
    for (const c of cues) { c.start = Math.max(0, c.start + d); c.end = Math.max(c.start + 0.1, c.end + d); }
    setDirty(true);
    renderList();
  }

  async function save() {
    try {
      cues.sort((a, b) => a.start - b.start);
      await put("/api/subtitles", { path, cues });
      setDirty(false);
      renderList();
      toast("已保存", "ok");
    } catch (e) { toast(e.message, "err"); }
  }

  async function reload() {
    if (dirty && !(await confirmDialog("放弃未保存的修改并重新加载？", { okText: "重新加载", danger: true }))) return;
    data = await get(`/api/subtitles?path=${encodeURIComponent(path)}`);
    cues = data.cues.map((c) => ({ start: c.start, end: c.end, text: c.text }));
    setDirty(false);
    renderList();
  }

  async function askAI() {
    if (dirty) await save();
    store.composerDraft = {
      text: `请校对字幕文件 ${path}：修正错别字、同音字、专有名词和标点，合理断句（每条不超过 16 个字），不要改变时间轴。改完后告诉我改了哪些地方。`,
      attachments: mediaPath ? [mediaPath] : [],
    };
    navigate("#/chat");
  }

  const onKey = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
    if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      const i = Math.max(0, Math.min(cues.length - 1, (active < 0 ? 0 : active) + (e.key === "ArrowDown" ? 1 : -1)));
      if (player) player.currentTime = cues[i].start + 0.01;
      list.children[i]?.querySelector("textarea")?.focus();
    }
  };
  const beforeUnload = (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
  document.addEventListener("keydown", onKey);
  window.addEventListener("beforeunload", beforeUnload);
  cleanup = () => {
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("beforeunload", beforeUnload);
    if (dirty) toast("字幕有未保存的修改（已离开编辑器）");
  };
  renderList();
}
