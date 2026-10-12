// Chat view: message thread, live streaming, tool cards, approvals, composer.
import { get, patch, post, rawUrl, stream, upload } from "./api.js";
import { autosize, basename, h, icon, kindOf, toast } from "./dom.js";
import { enhanceCode, markdown } from "./markdown.js";
import { previewFile } from "./preview.js";
import { anyProviderReady, modelLabel, modelOptions, navigate, on, providerName, refreshSessions, setActive, store } from "./store.js";

export const TOOL_META = {
  media_info: ["媒体信息", "info"], video_analyze: ["看懂视频", "film"], video_watch: ["观看片段", "play"],
  video_ask: ["Gemini 看视频", "sparkles"], video_trim: ["截取片段", "scissors"], video_cut: ["剪辑片段", "scissors"],
  detect_silence: ["检测静音", "wave"], remove_silence: ["剪掉静音", "scissors"], scene_detect: ["镜头检测", "film"],
  video_concat: ["拼接视频", "layers"], video_reframe: ["调整画幅", "film"], video_speed: ["变速", "zap"],
  video_convert: ["转码压缩", "film"], extract_audio: ["提取音频", "music"], view_frames: ["查看画面", "image"],
  snapshot: ["截取画面", "image"], make_gif: ["制作 GIF", "image"], ffmpeg_run: ["FFmpeg", "terminal"],
  video_compose: ["合成视频", "sparkles"], video_layout: ["分屏布局", "split"], video_effects: ["特效滤镜", "palette"],
  video_add_text: ["文字动画", "type"], add_overlay: ["叠加素材", "layers"], add_music: ["配乐", "music"],
  audio_beats: ["节拍分析", "wave"], audio_process: ["音频处理", "wave"], audio_visualize: ["音频可视化", "wave"],
  make_cover: ["生成封面", "image"], tts: ["AI 配音", "mic"], transcribe: ["语音识别", "mic"],
  subtitle_read: ["读取字幕", "captions"], subtitle_edit: ["编辑字幕", "edit"], subtitle_replace: ["批量替换", "edit"],
  subtitle_shift: ["调整时间轴", "clock"], subtitle_apply_texts: ["写入译文", "captions"],
  subtitle_resegment: ["重新断句", "captions"], subtitle_convert: ["字幕转换", "captions"],
  subtitle_burn: ["烧录字幕", "captions"], subtitle_embed: ["封装字幕", "captions"], list_files: ["查看文件", "folder"],
  read_file: ["读取文件", "text"], write_file: ["写入文件", "edit"], file_manage: ["文件管理", "folder"],
  run_command: ["执行命令", "terminal"], web_fetch: ["抓取网页", "globe"], memory_update: ["更新记忆", "brain"],
  download_media: ["下载视频", "download"], task_create: ["创建定时任务", "clock"], task_list: ["查看任务", "clock"],
  task_update: ["修改任务", "clock"], task_delete: ["删除任务", "clock"], notify: ["推送消息", "send"],
};

const SUGGESTIONS = [
  ["captions", "生成并校对字幕", "上传视频，自动识别语音、修正错别字并烧录字幕", "帮我给上传的视频生成中文字幕：识别后通读校对错别字和专有名词，然后用好看的样式烧录到视频上。"],
  ["scissors", "口播精剪", "剪掉停顿、气口和口误，加字幕", "把这个口播视频的停顿和气口剪掉，再根据字幕删掉明显的口误和重复，最后加上字幕。"],
  ["sparkles", "卡点混剪", "多段素材 + 音乐踩点 + 转场 + 调色", "用我上传的素材做一个 15 秒左右的竖屏卡点混剪：先看看每段画面挑精彩镜头，音乐卡点，加转场和电影感调色，开头放一个吸引人的标题。"],
  ["film", "横屏转竖屏", "9:16 模糊背景 + 标题 + 背景音乐", "把这个横屏视频转成 9:16 竖屏（模糊背景填充），顶部加一个标题，配上背景音乐（人声出现时自动压低）。"],
  ["mic", "AI 配音短视频", "图片/视频 + 文案 → 配音、字幕、成片", "用我上传的图片和下面这段文案做一个竖屏短视频：AI 配音、自动字幕、每张图片带缓慢推镜，转场自然。\n\n文案："],
  ["split", "分屏对比", "Before / After、画中画、宫格", "把这两个视频做成左右分屏对比视频，分别标上 Before 和 After。"],
  ["globe", "双语字幕", "翻译字幕并生成中英双语", "把这个视频的字幕翻译成英文，生成中英双语字幕（中文在上），并烧录一个双语版本。"],
  ["clock", "每日自动任务", "定时抓取资讯、处理文件、推送结果", "每天早上 9 点帮我抓取 Hacker News 首页热门内容，挑出最值得看的 10 条，整理成中文简报保存到 reports/ 文件夹。"],
];

let view = null;
on("settings", () => view?.refreshModel?.());

// ---------------------------------------------------------------------------
// Thread renderer

class Thread {
  constructor(container, scroller) {
    this.el = container;
    this.scroller = scroller;
    this.group = null;
    this.seg = null;
    this.cards = new Map();
    this.approvals = new Map();
    this.raf = 0;
  }

  nearBottom() {
    const s = this.scroller;
    return s.scrollHeight - s.scrollTop - s.clientHeight < 160;
  }
  scroll(force = false) {
    if (force || this.stick) this.scroller.scrollTop = this.scroller.scrollHeight;
  }
  mark() { this.stick = this.nearBottom(); }

  addUser(msg) {
    this.group = null;
    this.seg = null;
    const atts = (msg.attachments || []).map((p) => {
      const k = kindOf(p);
      return h("span", { class: "att" }, k === "image" ? h("img", { src: rawUrl(p) }) : h("span", { html: icon({ video: "film", audio: "music", subtitle: "captions" }[k] || "file") }),
        h("span", { class: "n" }, basename(p)));
    });
    const text = msg.text ?? (msg.content || "").replace(/\n\n\[用户附件\][\s\S]*$/, "");
    this.el.append(h("div", { class: "msg-user" }, h("div", {}, h("div", { class: "bubble" }, text), atts.length ? h("div", { class: "atts" }, atts) : null)));
  }

  ensureGroup(model) {
    if (!this.group) {
      const body = h("div", { class: "ai-body" });
      if (model) body.append(h("div", { class: "ai-meta" }, model));
      this.group = { body, el: h("div", { class: "msg-ai" }, h("div", { class: "avatar", html: icon("sparkles") }), body) };
      this.el.append(this.group.el);
    }
    return this.group;
  }

  startAssistant(model) {
    const g = this.ensureGroup(model);
    const md = h("div", { class: "md" });
    const seg = { md, text: "", reasoning: "", reasoningEl: null, box: h("div", {}, md) };
    g.body.append(seg.box);
    this.seg = seg;
    return seg;
  }

  reasoningDelta(d) {
    const seg = this.seg || this.startAssistant();
    seg.reasoning += d;
    if (!seg.reasoningEl) {
      const body = h("div", { class: "r-body" });
      seg.reasoningEl = h("details", { class: "reasoning", open: true },
        h("summary", { html: icon("chevron", "chev") + "思考中…" }), body);
      seg.box.prepend(seg.reasoningEl);
    }
    seg.reasoningEl.querySelector(".r-body").textContent = seg.reasoning;
  }

  delta(d) {
    const seg = this.seg || this.startAssistant();
    seg.text += d;
    if (seg.reasoningEl && seg.reasoningEl.open) {
      seg.reasoningEl.open = false;
      seg.reasoningEl.querySelector("summary").innerHTML = icon("chevron", "chev") + "思考过程";
    }
    if (!this.raf) {
      this.raf = requestAnimationFrame(() => {
        this.raf = 0;
        this.mark();
        seg.md.innerHTML = markdown(seg.text);
        seg.md.classList.add("cursor");
        this.scroll();
      });
    }
  }

  finishAssistant(msg) {
    const seg = this.seg || this.startAssistant(msg.model);
    this.mark();
    if (this.raf) { cancelAnimationFrame(this.raf); this.raf = 0; }
    seg.text = msg.content || "";
    seg.md.classList.remove("cursor");
    seg.md.innerHTML = markdown(seg.text);
    enhanceCode(seg.md);
    if (msg.reasoning && !seg.reasoningEl) {
      seg.reasoningEl = h("details", { class: "reasoning" }, h("summary", { html: icon("chevron", "chev") + "思考过程" }),
        h("div", { class: "r-body" }, msg.reasoning));
      seg.box.prepend(seg.reasoningEl);
    } else if (seg.reasoningEl) {
      seg.reasoningEl.open = false;
      seg.reasoningEl.querySelector("summary").innerHTML = icon("chevron", "chev") + "思考过程";
    }
    if (!seg.text.trim()) seg.md.remove();
    for (const tc of msg.tool_calls || []) {
      let args = tc.arguments;
      try { args = JSON.parse(tc.arguments || "{}"); } catch { /* keep raw */ }
      this.toolCard(tc.id, tc.name, args);
    }
    this.seg = null;
    this.scroll();
  }

  toolCard(id, name, args) {
    if (this.cards.has(id)) return this.cards.get(id);
    const g = this.ensureGroup();
    const [label, ico] = TOOL_META[name] || [name, "wand"];
    const state = h("div", { class: "tool-state" }, h("div", { class: "spinner" }));
    const bar = h("div", { class: "tool-progress indet" }, h("i", { style: { width: "0%" } }));
    const ptext = h("div", { class: "tool-progress-text" });
    const detail = h("div", { class: "tool-detail" },
      h("div", { class: "lbl" }, "参数"), h("pre", {}, typeof args === "string" ? args : JSON.stringify(args, null, 2)));
    const files = h("div", { class: "tool-files" });
    const head = h("div", { class: "tool-head", onclick: () => el.classList.toggle("open") },
      h("div", { class: "tool-ico", html: icon(ico) }),
      h("span", { class: "tool-name" }, label),
      h("span", { class: "tool-sum" }, summarize(name, args)),
      state);
    const el = h("div", { class: "tool" }, head, bar, ptext, detail, files);
    g.body.append(el);
    const card = { el, state, bar, ptext, detail, files, done: false };
    this.cards.set(id, card);
    this.scroll();
    return card;
  }

  toolProgress(id, p, text) {
    const c = this.cards.get(id);
    if (!c || c.done) return;
    c.bar.classList.remove("indet");
    c.bar.firstChild.style.width = `${Math.round(p * 100)}%`;
    c.state.innerHTML = `<div class="spinner"></div>${Math.round(p * 100)}%`;
    if (text) c.ptext.textContent = text;
  }

  toolResult(msg) {
    this.mark();
    const c = this.toolCard(msg.tool_call_id, msg.name, msg.arguments || {});
    c.done = true;
    c.bar.remove();
    c.ptext.remove();
    const ok = msg.ok !== false;
    c.state.innerHTML = msg.denied ? '<span class="bad">已拒绝</span>' : ok ? `<span class="ok">${icon("check")}</span>` : `<span class="bad">${icon("x")}</span>`;
    if (!ok) c.el.classList.add("err");
    c.detail.append(h("div", { class: "lbl" }, "结果"), h("pre", {}, msg.content || ""));
    const shown = new Set();
    const editorMedia = msg.editor?.media;
    for (const f of [...(msg.files || []), ...(msg.images || [])]) {
      if (shown.has(f)) continue;
      shown.add(f);
      c.files.append(previewFile(f, { editorMedia }));
    }
    const ap = this.approvals.get(msg.tool_call_id);
    if (ap) { ap.remove(); this.approvals.delete(msg.tool_call_id); }
    this.scroll();
  }

  approval(ev, sid) {
    const g = this.ensureGroup();
    const args = ev.arguments || {};
    const body = args.command ? args.command : JSON.stringify(args, null, 2);
    const btns = h("div", { class: "row" });
    const el = h("div", { class: "approval" },
      h("div", { class: "row", html: icon("alert") + "<b>需要你的批准</b>" }),
      h("div", { class: "small muted", style: { marginTop: "4px" } }, `Agent 想要执行「${(TOOL_META[ev.name] || [ev.name])[0]}」：`),
      h("pre", {}, body), btns);
    const decide = async (approved) => {
      btns.innerHTML = '<span class="muted small">已提交…</span>';
      try { await post(`/api/sessions/${sid}/approve`, { id: ev.id, approved }); } catch (e) { toast(e.message, "err"); }
    };
    btns.append(h("button", { class: "btn primary sm", onclick: () => decide(true) }, "允许执行"),
      h("button", { class: "btn sm", onclick: () => decide(false) }, "拒绝"),
      h("a", { class: "small muted", href: "#/settings/general", style: { marginLeft: "8px" } }, "权限设置"));
    g.body.append(el);
    this.approvals.set(ev.id, el);
    this.scroll(true);
  }

  approvalResolved(id) {
    const el = this.approvals.get(id);
    if (el) { el.remove(); this.approvals.delete(id); }
  }

  notice(text, kind = "") {
    const target = this.group ? this.group.body : this.el;
    target.append(h("div", { class: `notice ${kind}`, html: icon(kind === "err" ? "alert" : "info") }, h("div", {}, text)));
    this.scroll(true);
  }

  renderHistory(msgs) {
    for (const m of msgs) {
      if (m.role === "user") this.addUser(m);
      else if (m.role === "assistant") { this.startAssistant(m.model); this.finishAssistant(m); }
      else if (m.role === "tool") this.toolResult(m);
    }
  }

  handle(ev, sid) {
    switch (ev.type) {
      case "user_message": this.addUser(ev.message); this.scroll(true); break;
      case "assistant_start": this.mark(); this.startAssistant(ev.model); break;
      case "text_delta": this.delta(ev.delta); break;
      case "reasoning_delta": this.reasoningDelta(ev.delta); break;
      case "assistant_done": this.finishAssistant(ev.message); break;
      case "tool_start": this.mark(); this.toolCard(ev.id, ev.name, ev.arguments); break;
      case "tool_progress": this.toolProgress(ev.id, ev.progress, ev.text); break;
      case "tool_result": this.toolResult(ev.message); break;
      case "approval_request": this.approval(ev, sid); break;
      case "approval_resolved": this.approvalResolved(ev.id); break;
      case "notice": this.notice(ev.text); break;
      case "error": this.notice(ev.message, "err"); this.cleanupStreaming(); break;
      case "cancelled": this.notice("已停止"); this.cleanupStreaming(); break;
      default: break;
    }
  }

  cleanupStreaming() {
    if (this.seg) { this.seg.md.classList.remove("cursor"); if (!this.seg.text.trim()) this.seg.md.remove(); this.seg = null; }
    for (const c of this.cards.values()) {
      if (!c.done) { c.done = true; c.bar.remove(); c.ptext.remove(); c.state.innerHTML = '<span class="muted">已中断</span>'; }
    }
  }
}

function summarize(name, args) {
  if (!args || typeof args !== "object") return "";
  const pick = args.input ?? args.path ?? args.video ?? args.source ?? args.url ?? args.command ?? args.name;
  if (Array.isArray(args.inputs)) return args.inputs.map(basename).join(" + ");
  if (name === "video_compose") return `${(args.clips || []).length} 个片段${args.music ? " · 配乐" : ""}${args.size ? " · " + args.size : ""}`;
  if (name === "tts") return String(args.text || "").slice(0, 40);
  if (pick) return typeof pick === "string" && /[\\/]/.test(pick) && !/^https?:/.test(pick) ? basename(pick) : String(pick).slice(0, 80);
  return "";
}

// ---------------------------------------------------------------------------
// Model picker

function modelPicker(getCurrent, onPick) {
  const btn = h("button", { class: "btn sm" });
  const wrap = h("div", { class: "picker" }, btn);
  let menu = null;
  const label = () => {
    const { pid, model } = getCurrent();
    btn.innerHTML = `${icon("bot")}<span>${providerName(pid)} · ${modelLabel(pid, model) || "未选择"}</span>${icon("down")}`;
  };
  const close = () => { menu?.remove(); menu = null; document.removeEventListener("mousedown", outside); };
  const outside = (e) => { if (!wrap.contains(e.target)) close(); };
  btn.onclick = () => {
    if (menu) return close();
    const cur = getCurrent();
    menu = h("div", { class: "picker-menu" });
    const opts = modelOptions();
    const groups = {};
    for (const o of opts) (groups[o.pid] ||= []).push(o);
    for (const [pid, list] of Object.entries(groups)) {
      const ready = list[0].ready;
      menu.append(h("div", { class: "grp" }, list[0].name + (ready ? "" : "（未连接）")));
      for (const o of list) {
        const sel = o.pid === cur.pid && o.model === cur.model;
        menu.append(h("div", { class: `opt ${sel ? "sel" : ""}`, style: { opacity: ready ? 1 : 0.5 },
          onclick: () => { close(); if (!ready) { navigate("#/settings/models"); return; } onPick(o.pid, o.model); } },
          h("span", { html: sel ? icon("check") : '<span style="width:14px;display:inline-block"></span>' }), o.label));
      }
    }
    menu.append(h("div", { class: "opt", onclick: () => { close(); navigate("#/settings/models"); } }, h("span", { html: icon("settings") }), "管理模型…"));
    wrap.append(menu);
    setTimeout(() => document.addEventListener("mousedown", outside));
  };
  label();
  return { el: wrap, refresh: label };
}

// ---------------------------------------------------------------------------
// Composer

function composer({ onSend, onStop }) {
  const atts = [];
  const attBox = h("div", { class: "composer-atts", style: { display: "none" } });
  const ta = h("textarea", { rows: 1, placeholder: "描述你想做的事，例如：把 uploads 里的视频剪成 30 秒竖屏精华版，加字幕和音乐…" });
  const fileInput = h("input", { type: "file", multiple: true, style: { display: "none" } });
  const sendBtn = h("button", { class: "btn primary icon-only", title: "发送 (Enter)", html: icon("up") });
  let running = false;

  const renderAtts = () => {
    attBox.innerHTML = "";
    attBox.style.display = atts.length ? "flex" : "none";
    atts.forEach((a, i) => {
      attBox.append(h("span", { class: "att" },
        a.uploading ? h("div", { class: "spinner" }) : a.kind === "image" ? h("img", { src: rawUrl(a.path) }) : h("span", { html: icon({ video: "film", audio: "music", subtitle: "captions" }[a.kind] || "file") }),
        h("span", { class: "n", title: a.path || a.name }, a.name),
        h("button", { html: icon("x"), onclick: () => { atts.splice(i, 1); renderAtts(); } })));
    });
  };
  const addFiles = async (files) => {
    const pending = [...files].map((f) => ({ name: f.name, uploading: true }));
    atts.push(...pending);
    renderAtts();
    try {
      const res = await upload(files);
      res.forEach((r, i) => Object.assign(pending[i], { path: r.path, kind: r.kind, name: r.name, uploading: false }));
    } catch (e) {
      toast("上传失败：" + e.message, "err");
      pending.forEach((p) => atts.splice(atts.indexOf(p), 1));
    }
    renderAtts();
  };
  const addPaths = (paths) => {
    for (const p of paths) atts.push({ path: p, name: basename(p), kind: kindOf(p) });
    renderAtts();
  };
  const send = () => {
    if (running) return;
    if (atts.some((a) => a.uploading)) return toast("文件还在上传中…");
    const text = ta.value.trim();
    if (!text && !atts.length) return;
    const paths = atts.map((a) => a.path);
    onSend(text, paths);
    ta.value = "";
    atts.length = 0;
    renderAtts();
    autosize(ta);
  };
  ta.addEventListener("input", () => autosize(ta));
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  ta.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  fileInput.onchange = () => { if (fileInput.files.length) addFiles(fileInput.files); fileInput.value = ""; };
  sendBtn.onclick = () => (running ? onStop() : send());

  const el = h("div", { class: "composer-wrap" },
    h("div", { class: "composer" }, attBox, ta,
      h("div", { class: "composer-bar" },
        h("button", { class: "btn ghost sm icon-only", title: "添加文件（也可拖拽或粘贴）", html: icon("clip"), onclick: () => fileInput.click() }),
        h("button", { class: "btn ghost sm", title: "从工作区选择", html: icon("folder") + "工作区", onclick: () => navigate("#/files") }),
        h("div", { class: "grow" }),
        h("span", { class: "small muted" }, h("kbd", {}, "Enter"), " 发送 · ", h("kbd", {}, "Shift+Enter"), " 换行"),
        sendBtn, fileInput)));
  return {
    el, ta, addFiles, addPaths,
    setText: (t) => { ta.value = t; autosize(ta); ta.focus(); ta.setSelectionRange(t.length, t.length); },
    setRunning(r) {
      running = r;
      sendBtn.innerHTML = r ? icon("stop") : icon("up");
      sendBtn.title = r ? "停止" : "发送 (Enter)";
      sendBtn.classList.toggle("primary", !r);
    },
  };
}

// ---------------------------------------------------------------------------
// Page

function dropZone(main, comp) {
  const overlay = h("div", { class: "drop-overlay" }, h("div", { html: icon("upload") + " 松开以添加文件" }));
  main.append(overlay);
  let depth = 0;
  main.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types?.includes("Files")) { depth++; overlay.classList.add("show"); } });
  main.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) overlay.classList.remove("show"); });
  main.addEventListener("dragover", (e) => e.preventDefault());
  main.addEventListener("drop", (e) => {
    e.preventDefault(); depth = 0; overlay.classList.remove("show");
    if (e.dataTransfer?.files?.length) comp.addFiles(e.dataTransfer.files);
  });
}

function welcome(onPick) {
  const ready = anyProviderReady();
  const wrap = h("div", { class: "welcome" },
    h("h2", {}, ready ? "今天想创作点什么？" : "欢迎使用 ", ready ? null : h("span", { class: "grad-text" }, "AutoStudio")),
    h("p", { class: "lead" }, ready ? "上传素材，用一句话告诉我你想要的效果。我会自己剪辑、配乐、加字幕和特效。"
      : "先连接一个大模型。两种方式任选其一："));
  if (!ready) {
    wrap.append(h("div", { class: "onboard" },
      h("div", { class: "card", onclick: () => navigate("#/settings/models?login=chatgpt") },
        h("div", { class: "big", html: icon("zap") }), h("h3", {}, "使用 ChatGPT 订阅"),
        h("div", { class: "small muted" }, "登录你的 ChatGPT Plus / Pro 账户，直接使用订阅额度，无需 API Key。")),
      h("div", { class: "card", onclick: () => navigate("#/settings/models?add=1") },
        h("div", { class: "big", html: icon("key") }), h("h3", {}, "使用 API Key"),
        h("div", { class: "small muted" }, "OpenAI、Claude、Gemini、DeepSeek、通义千问、Kimi、智谱、豆包、OpenRouter、Ollama…按 token 计费。"))));
  }
  wrap.append(h("div", { class: "suggest" }, SUGGESTIONS.map(([ico, title, desc, prompt]) =>
    h("button", { class: "s", onclick: () => onPick(prompt) }, h("div", { class: "ico", html: icon(ico) }),
      h("div", {}, h("b", {}, title), h("span", {}, desc))))));
  return wrap;
}

export async function showChat(main, sid) {
  if (view?.es) view.es.close();
  view = null;
  main.innerHTML = "";
  let session = null;
  if (sid) {
    try { session = await get(`/api/sessions/${sid}`); } catch (e) { toast(e.message, "err"); navigate("#/chat"); return; }
  }
  const scroller = h("div", { class: "chat-scroll" });
  const inner = h("div", { class: "chat-inner" });
  scroller.append(inner);
  const thread = new Thread(inner, scroller);
  const st = { session, es: null, running: !!session?.running };
  view = st;

  const current = () => ({
    pid: st.session?.provider || store.settings?.active?.provider,
    model: st.session?.model || store.settings?.active?.model,
  });
  const picker = modelPicker(current, async (pid, model) => {
    try {
      if (st.session) st.session = { ...st.session, ...(await patch(`/api/sessions/${st.session.id}`, { provider: pid, model })) };
      await setActive(pid, model);
      picker.refresh();
    } catch (e) { toast(e.message, "err"); }
  });
  st.refreshModel = () => picker.refresh();

  const titleEl = h("h1", { class: "title-edit", title: "点击重命名" }, session?.title || "新对话");
  titleEl.onclick = () => {
    if (!st.session) return;
    const input = h("input", { class: "input", value: st.session.title, style: { maxWidth: "420px", height: "32px" } });
    titleEl.replaceWith(input);
    input.focus();
    input.select();
    const done = async () => {
      const t = input.value.trim() || st.session.title;
      input.replaceWith(titleEl);
      titleEl.textContent = t;
      if (t !== st.session.title) { await patch(`/api/sessions/${st.session.id}`, { title: t }); st.session.title = t; refreshSessions(); }
    };
    input.onblur = done;
    input.onkeydown = (e) => { if (e.key === "Enter") input.blur(); if (e.key === "Escape") { input.value = st.session.title; input.blur(); } };
  };
  const head = h("div", { class: "page-head" },
    h("button", { class: "btn ghost icon-only menu-btn", html: icon("menu"), onclick: () => document.getElementById("sidebar").classList.toggle("open") }),
    titleEl, h("div", { class: "grow" }), picker.el);

  const comp = composer({
    onSend: (text, paths) => send(text, paths),
    onStop: async () => { if (st.session) await post(`/api/sessions/${st.session.id}/stop`); },
  });

  const subscribe = (after) => {
    st.running = true;
    comp.setRunning(true);
    st.es = stream(`/api/sessions/${st.session.id}/events?after=${after}`, (ev) => {
      if (ev.type === "title") { titleEl.textContent = ev.title; refreshSessions(); }
      thread.handle(ev, st.session.id);
    }, () => {
      st.running = false;
      comp.setRunning(false);
      thread.cleanupStreaming();
      refreshSessions();
    });
  };

  const send = async (text, paths) => {
    try {
      if (!st.session) {
        st.session = await post("/api/sessions", { provider: current().pid, model: current().model });
        history.replaceState(null, "", `#/chat/${st.session.id}`);
        inner.innerHTML = "";
      }
      await post(`/api/sessions/${st.session.id}/messages`, { content: text, attachments: paths });
      refreshSessions();
      subscribe(-1);
    } catch (e) {
      toast(e.message, "err");
      if (/找不到模型|没有设置模型|尚未登录|API Key|Base URL/.test(e.message)) navigate("#/settings/models");
    }
  };

  main.append(head, scroller, comp.el);
  dropZone(main, comp);

  if (!session) {
    inner.append(welcome((p) => comp.setText(p)));
  } else {
    const msgs = session.messages || [];
    if (session.running) {
      let idx = msgs.length;
      for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === "user") { idx = i; break; }
      thread.renderHistory(msgs.slice(0, idx));
      subscribe(-1);
    } else {
      thread.renderHistory(msgs);
    }
    requestAnimationFrame(() => thread.scroll(true));
  }
  const draft = store.composerDraft;
  if (draft) {
    store.composerDraft = null;
    if (draft.attachments?.length) comp.addPaths(draft.attachments);
    if (draft.text) comp.setText(draft.text);
    if (draft.autoSend) send(draft.text, draft.attachments || []);
  }
  comp.ta.focus();
}

export function stopChatView() {
  if (view?.es) view.es.close();
  view = null;
}
