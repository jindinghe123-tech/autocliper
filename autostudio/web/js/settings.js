// Settings: model providers (ChatGPT sign-in / API keys), speech, general, tools, about.
import { get, post, put, rawUrl, upload } from "./api.js";
import { confirmDialog, h, icon, modal, toast } from "./dom.js";
import { emit, modelOptions, navigate, providerReady, refreshSettings, setActive, store } from "./store.js";

const TABS = [["models", "模型"], ["voice", "语音"], ["assets", "素材"], ["general", "通用"], ["tools", "工具"], ["about", "关于"]];
const LOGO_COLORS = { chatgpt: "#10a37f", openai: "#10a37f", anthropic: "#d97757", gemini: "#4285f4", deepseek: "#4d6bfe",
  qwen: "#615ced", moonshot: "#16191e", zhipu: "#3859ff", doubao: "#1e6fff", siliconflow: "#7c3aed", openrouter: "#6467f2", ollama: "#333", custom: "#64748b" };
const TYPE_LABEL = { chatgpt: "ChatGPT 订阅", openai_chat: "OpenAI 兼容", openai_responses: "OpenAI Responses", anthropic: "Anthropic" };

let pollTimer = 0;

export async function showSettings(main, tab = "models", params = new URLSearchParams()) {
  clearInterval(pollTimer);
  main.innerHTML = "";
  await refreshSettings();
  const body = h("div", { class: "page-narrow" });
  const tabs = h("div", { class: "tabs" }, TABS.map(([k, l]) =>
    h("button", { class: k === tab ? "active" : "", onclick: () => navigate(`#/settings/${k}`) }, l)));
  main.append(h("div", { class: "page-head" },
    h("button", { class: "btn ghost icon-only menu-btn", html: icon("menu"), onclick: () => document.getElementById("sidebar").classList.toggle("open") }),
    h("h1", {}, "设置")), h("div", { class: "page-body" }, h("div", { class: "page-narrow" }, tabs, body)));
  const render = { models: renderModels, voice: renderVoice, assets: renderAssets, general: renderGeneral, tools: renderTools, about: renderAbout }[tab] || renderModels;
  render(body, params);
}

// ---------------------------------------------------------------------------
// Models

function logo(key, text) {
  return h("div", { class: "prov-logo", style: { background: LOGO_COLORS[key] || "#64748b" } }, text);
}

function renderModels(body, params) {
  body.innerHTML = "";
  const s = store.settings;
  const opts = modelOptions().filter((o) => o.ready);
  const active = s.active || {};
  const defSel = h("select", { class: "select", style: { maxWidth: "420px" } },
    opts.length ? opts.map((o) => h("option", { value: `${o.pid}|${o.model}`, selected: o.pid === active.provider && o.model === active.model }, `${o.name} · ${o.model}`))
      : h("option", {}, "还没有可用的模型，请先在下方连接"));
  defSel.onchange = async () => { const [p, m] = defSel.value.split("|"); await setActive(p, m); toast("已设为默认模型", "ok"); };
  body.append(h("div", { class: "field" }, h("label", {}, "默认模型"), defSel,
    h("div", { class: "hint" }, "新对话默认使用该模型；每个对话也可以在右上角单独切换。")));

  body.append(h("div", { class: "section-title" }, "方式一：使用 ChatGPT 订阅（Plus / Pro / Team）"));
  body.append(chatgptCard(params.get("login") === "chatgpt"));

  body.append(h("div", { class: "section-title" }, "方式二：使用 API Key（按 token 计费）",
    h("div", { class: "grow" }), h("button", { class: "btn sm primary", html: icon("plus") + "添加提供方", onclick: () => addProvider() })));
  const provs = Object.entries(s.providers || {}).filter(([, p]) => p.type !== "chatgpt");
  if (!provs.length) {
    body.append(h("div", { class: "card empty" }, "还没有添加 API 提供方。支持 OpenAI、Claude、Gemini、DeepSeek、通义千问、Kimi、智谱、豆包、硅基流动、OpenRouter、Ollama 以及任何 OpenAI 兼容接口。",
      h("div", { style: { marginTop: "12px" } }, h("button", { class: "btn primary", html: icon("plus") + "添加提供方", onclick: () => addProvider() }))));
  }
  for (const [pid, p] of provs) body.append(providerCard(pid, p));
  if (params.get("add") === "1") setTimeout(() => addProvider(), 50);
}

const EFFORT_LABELS = { auto: "自动（模型默认，推荐）", minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "很高", max: "极高", ultra: "最高（最慢）" };
let catalogRefreshed = false;

function chatgptCard(autoLogin) {
  const st = store.chatgpt || {};
  const cfg = store.settings.providers.chatgpt;
  const catalog = cfg.catalog || [];
  const active = store.settings.active || {};
  const card = h("div", { class: "card" });
  card.append(h("div", { class: "prov-card" }, logo("chatgpt", "GPT"),
    h("div", { class: "grow" },
      h("h3", {}, "ChatGPT 账户 ", st.logged_in ? h("span", { class: "tag ok" }, "已登录") : h("span", { class: "tag" }, "未登录")),
      st.logged_in
        ? h("div", { class: "small muted" }, `${st.email || "ChatGPT 用户"}${st.plan ? " · " + st.plan.toUpperCase() : ""}`)
        : h("div", { class: "small muted" }, "使用与 OpenAI Codex CLI 相同的官方登录流程。登录后对话消耗的是你的 ChatGPT 订阅额度，不需要 API Key。"))));

  const actions = h("div", { class: "row wrap", style: { marginTop: "14px" } });
  const waiting = h("div", { style: { display: "none", marginTop: "14px" } });
  const refreshModels = async (btn, quiet = false) => {
    if (btn) { btn.disabled = true; btn.innerHTML = '<div class="spinner"></div>获取中'; }
    try {
      const r = await post("/api/providers/chatgpt/models");
      catalogRefreshed = true;
      await refreshSettings();
      if (!quiet) toast(`已获取 ${r.models.length} 个可用模型`, "ok");
      navigate("#/settings/models");
    } catch (e) {
      if (!quiet) toast(e.message, "err");
      if (btn) { btn.disabled = false; btn.innerHTML = icon("refresh") + "刷新模型列表"; }
    }
  };
  if (st.logged_in) {
    actions.append(
      h("button", { class: "btn sm", html: icon("zap") + "测试连接", onclick: (e) => testProvider("chatgpt", e.currentTarget) }),
      h("button", { class: "btn sm", html: icon("refresh") + "刷新模型列表", onclick: (e) => refreshModels(e.currentTarget) }),
      h("button", { class: "btn sm danger", html: icon("logout") + "退出登录", onclick: async () => {
        if (!(await confirmDialog("退出 ChatGPT 登录？本地保存的令牌会被删除。"))) return;
        await post("/api/auth/chatgpt/logout"); await refreshSettings(); navigate("#/settings/models");
      } }));
    // fetch the live list once per page load so new OpenAI models appear automatically
    if (!catalogRefreshed) { catalogRefreshed = true; setTimeout(() => refreshModels(null, true), 50); }
  } else {
    actions.append(h("button", { class: "btn primary", html: icon("zap") + "登录 ChatGPT", onclick: () => startLogin() }),
      h("button", { class: "btn", html: icon("download") + "从 Codex CLI 导入登录", title: "如果你已经用 codex login 登录过", onclick: async () => {
        try { await post("/api/auth/chatgpt/import"); await afterLogin(); toast("导入成功", "ok"); } catch (e) { toast(e.message, "err"); }
      } }));
    if (autoLogin) setTimeout(() => startLogin(), 100);
  }
  card.append(actions, waiting);

  async function afterLogin() {
    try { await post("/api/providers/chatgpt/models"); } catch { /* keep the built-in fallback list */ }
    await refreshSettings();
    const a = store.settings.active || {};
    if (a.provider === "chatgpt" || !providerReady(a.provider, store.settings.providers?.[a.provider]))
      await setActive("chatgpt", (store.settings.providers.chatgpt.models || [])[0]);
    navigate("#/settings/models");
  }

  async function startLogin() {
    let r;
    try { r = await post("/api/auth/chatgpt/login"); } catch (e) { return toast(e.message, "err"); }
    window.open(r.auth_url, "_blank", "noopener");
    const paste = h("input", { class: "input", placeholder: "http://localhost:1455/auth/callback?code=...&state=..." });
    waiting.style.display = "block";
    waiting.innerHTML = "";
    waiting.append(
      h("div", { class: "notice" }, h("div", { class: "spinner" }), h("div", {},
        h("b", {}, "请在新打开的页面中登录 ChatGPT"), h("div", { class: "small muted" }, "登录完成后会自动跳回，这里会自动更新状态。"),
        r.error ? h("div", { class: "small", style: { color: "var(--warn)" } }, r.error) : null,
        h("div", { class: "small", style: { marginTop: "6px" } }, h("a", { href: r.auth_url, target: "_blank", rel: "noopener" }, "没有弹出？点这里打开登录页")))),
      h("details", { style: { marginTop: "8px" } }, h("summary", { class: "small muted", style: { cursor: "pointer" } }, "登录后页面打不开 / 在远程服务器上运行？手动粘贴回调地址"),
        h("div", { class: "row", style: { marginTop: "8px" } }, paste, h("button", { class: "btn sm", onclick: async () => {
          try { await post("/api/auth/chatgpt/callback", { url: paste.value }); } catch (e) { toast(e.message, "err"); }
        } }, "提交"))));
    clearInterval(pollTimer);
    const started = Date.now();
    pollTimer = setInterval(async () => {
      const s = await get("/api/auth/chatgpt").catch(() => null);
      if (s?.logged_in) {
        clearInterval(pollTimer);
        toast("ChatGPT 登录成功！正在获取可用模型…", "ok");
        await afterLogin();
      } else if (Date.now() - started > 600000) clearInterval(pollTimer);
    }, 2000);
  }

  // ---- live model list (click to make default)
  if (st.logged_in) {
    const list = h("div", { style: { display: "grid", gap: "8px", marginTop: "10px" } });
    const rows = catalog.length ? catalog : (cfg.models || []).map((slug) => ({ slug, display_name: slug, description: "" }));
    for (const m of rows) {
      const isDefault = active.provider === "chatgpt" && active.model === m.slug;
      list.append(h("div", {
        class: "side-item",
        style: { border: "1px solid var(--border)", padding: "10px 12px", ...(isDefault ? { borderColor: "var(--accent)", background: "var(--accent-soft)" } : {}) },
        title: "设为默认模型",
        onclick: async () => { await setActive("chatgpt", m.slug); toast(`默认模型：${m.display_name}`, "ok"); navigate("#/settings/models"); },
      },
      h("div", { class: "grow", style: { minWidth: 0 } },
        h("div", {}, h("b", {}, m.display_name), " ", h("span", { class: "mono small muted" }, m.slug), " ",
          isDefault ? h("span", { class: "tag accent" }, "默认") : null,
          m.context_window ? h("span", { class: "tag", style: { marginLeft: "4px" } }, `${Math.round(m.context_window / 1000)}K 上下文`) : null),
        m.description ? h("div", { class: "small muted" }, m.description) : null)));
    }
    card.append(h("div", { class: "section-title", style: { margin: "18px 0 0" } }, "你的账户可用的模型",
      h("span", { class: "small muted", style: { fontWeight: 400 } }, catalog.length ? "（实时获取自 OpenAI，点击设为默认）" : "（正在获取…）")), list);
  }

  const efforts = [...new Set(catalog.flatMap((m) => m.efforts || []))];
  const effortList = ["auto", ...(efforts.length ? efforts : ["low", "medium", "high", "xhigh"])];
  const effort = h("select", { class: "select", style: { width: "220px" } },
    effortList.map((e) => h("option", { value: e, selected: (cfg.reasoning_effort || "auto") === e }, EFFORT_LABELS[e] || e)));
  effort.onchange = async () => {
    await put("/api/settings", { providers: { chatgpt: { reasoning_effort: effort.value } } });
    await refreshSettings();
    toast("推理强度已保存", "ok");
  };
  const custom = h("input", { class: "input", value: (cfg.models || []).join(", ") });
  card.append(
    h("div", { class: "row wrap", style: { marginTop: "14px" } }, h("span", { class: "small muted" }, "推理强度"), effort,
      h("span", { class: "small muted" }, "越高越聪明，但更慢、更耗额度；模型不支持的档位会自动换成最接近的一档")),
    h("details", { style: { marginTop: "12px" } },
      h("summary", { class: "small muted", style: { cursor: "pointer" } }, "高级：手动指定模型名"),
      h("div", { style: { marginTop: "10px" } },
        h("div", { class: "field" }, custom, h("div", { class: "hint" }, "逗号分隔。可以填列表里没有的隐藏/旧模型（如 gpt-5.5）。刷新模型列表时会被覆盖。")),
        h("button", { class: "btn sm", onclick: async () => {
          await put("/api/settings", { providers: { chatgpt: { models: custom.value.split(/[,，\s]+/).filter(Boolean) } } });
          await refreshSettings(); toast("已保存", "ok"); navigate("#/settings/models");
        } }, "保存"))),
    h("div", { class: "small muted", style: { marginTop: "12px", lineHeight: 1.6 } },
      "说明：该方式调用的是 ChatGPT/Codex 后端（非公开文档的接口），可能随 OpenAI 调整而变化；令牌仅保存在本机。请仅用于你自己的账户并遵守 OpenAI 使用条款。"));
  return card;
}

function providerCard(pid, p) {
  const ready = providerReady(pid, p);
  return h("div", { class: "card" }, h("div", { class: "prov-card" },
    logo(p.preset || "custom", (p.name || pid).slice(0, 2)),
    h("div", { class: "grow" },
      h("h3", {}, p.name || pid, " ", h("span", { class: "tag" }, TYPE_LABEL[p.type] || p.type), " ",
        ready ? h("span", { class: "tag ok" }, "可用") : h("span", { class: "tag warn" }, "缺少 Key")),
      h("div", { class: "small muted mono" }, p.base_url || ""),
      h("div", { class: "small muted", style: { marginTop: "4px" } }, `模型：${(p.models || []).join(", ") || "（未设置）"}`,
        p.api_key_hint ? ` · Key ${p.api_key_hint}` : "", p.vision ? " · 支持图片" : ""),
      h("div", { class: "row wrap", style: { marginTop: "12px" } },
        h("button", { class: "btn sm", html: icon("zap") + "测试", onclick: (e) => testProvider(pid, e.currentTarget) }),
        h("button", { class: "btn sm", html: icon("edit") + "编辑", onclick: () => providerForm(pid, p) }),
        ready ? h("button", { class: "btn sm", html: icon("check") + "设为默认", onclick: async () => { await setActive(pid, (p.models || [])[0]); toast("已设为默认", "ok"); navigate("#/settings/models"); } }) : null,
        h("button", { class: "btn sm danger", html: icon("trash"), onclick: async () => {
          if (!(await confirmDialog(`删除「${p.name || pid}」？`, { danger: true, okText: "删除" }))) return;
          await put("/api/settings", { providers: { [pid]: null } });
          navigate("#/settings/models");
        } })))));
}

async function testProvider(pid, btn) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div>测试中';
  try {
    const r = await post(`/api/providers/${pid}/test`, {});
    toast(`连接成功（${r.model}，${r.latency}s）：${r.reply}`, "ok");
  } catch (e) { toast("连接失败：" + e.message, "err"); }
  btn.disabled = false;
  btn.innerHTML = old;
}

function addProvider() {
  const grid = h("div", { class: "suggest" }, (store.meta?.presets || []).map((pr) =>
    h("button", { class: "s", onclick: () => { m.close(); providerForm(null, { ...pr, api_key_set: false }); } },
      logo(pr.preset, pr.name.slice(0, 2)), h("div", {}, h("b", {}, pr.name), h("span", {}, TYPE_LABEL[pr.type])))));
  const m = modal({ title: "选择模型提供方", body: grid, wide: true });
}

function providerForm(pid, p) {
  const isNew = !pid;
  const name = h("input", { class: "input", value: p.name || "" });
  const type = h("select", { class: "select" }, Object.entries(TYPE_LABEL).filter(([k]) => k !== "chatgpt").map(([k, l]) =>
    h("option", { value: k, selected: p.type === k }, l)));
  const base = h("input", { class: "input mono", value: p.base_url || "", placeholder: "https://…/v1" });
  const key = h("input", { class: "input mono", type: "password", placeholder: p.api_key_set ? `已保存（${p.api_key_hint}），留空则不修改` : "sk-…", autocomplete: "off" });
  const models = h("input", { class: "input", value: (p.models || []).join(", "), placeholder: "模型名，逗号分隔，第一个为默认" });
  const vision = h("input", { type: "checkbox", checked: !!p.vision });
  const fetchBtn = h("button", { class: "btn sm", html: icon("refresh") + "获取模型列表" });
  const modelList = h("div", { class: "row wrap", style: { gap: "6px", marginTop: "6px" } });

  const collect = () => {
    const out = { name: name.value.trim() || p.name, type: type.value, base_url: base.value.trim(),
      models: models.value.split(/[,，\s]+/).filter(Boolean), vision: vision.checked, preset: p.preset || "custom" };
    if (p.reasoning_echo) out.reasoning_echo = true;
    if (key.value.trim()) out.api_key = key.value.trim();
    return out;
  };
  const save = async () => {
    const data = collect();
    let id = pid;
    if (!id) { // first save of a new provider: allocate an id once, then keep updating it
      const baseId = (p.preset || "custom").replace(/[^\w-]/g, "");
      id = baseId;
      let n = 2;
      while (store.settings.providers[id]) id = `${baseId}-${n++}`;
      pid = id;
    }
    await put("/api/settings", { providers: { [id]: data } });
    await refreshSettings();
    return id;
  };
  fetchBtn.onclick = async () => {
    fetchBtn.disabled = true;
    try {
      const id = await save();
      pid = id;
      const r = await post(`/api/providers/${id}/models`);
      modelList.innerHTML = "";
      const list = r.models.slice(0, 200);
      if (!list.length) modelList.append(h("span", { class: "small muted" }, "接口没有返回模型列表，请手动填写"));
      for (const mname of list) {
        modelList.append(h("button", { class: "tag", style: { cursor: "pointer" }, onclick: () => {
          const cur = models.value.split(/[,，\s]+/).filter(Boolean);
          if (!cur.includes(mname)) models.value = [...cur, mname].join(", ");
        } }, mname));
      }
    } catch (e) { toast(e.message, "err"); }
    fetchBtn.disabled = false;
  };
  const m = modal({
    title: isNew ? `添加 ${p.name}` : `编辑 ${p.name}`,
    body: h("div", {},
      h("div", { class: "row" }, h("div", { class: "field grow" }, h("label", {}, "名称"), name),
        h("div", { class: "field", style: { width: "200px" } }, h("label", {}, "接口协议"), type)),
      h("div", { class: "field" }, h("label", {}, "Base URL"), base),
      h("div", { class: "field" }, h("label", {}, "API Key"), key,
        h("div", { class: "hint" }, "仅保存在本机 ~/.autostudio/settings.json，只会发送给上面的 Base URL。Ollama 等本地服务可留空。")),
      h("div", { class: "field" }, h("label", {}, "模型"), models, h("div", { class: "row" }, fetchBtn), modelList),
      h("label", { class: "row small", style: { gap: "8px", cursor: "pointer" } }, vision, "模型支持图片输入（开启后 Agent 可以“看”视频画面）")),
    footer: [h("button", { class: "btn", onclick: () => m.close() }, "取消"),
      h("button", { class: "btn primary", onclick: async () => {
        try {
          let id = await save();
          // pull the provider's live model list (newest first) so new providers never start with stale names
          const prov = store.settings.providers[id];
          if (providerReady(id, prov) && (isNew || !collect().models.length)) {
            try {
              const r = await post(`/api/providers/${id}/models`);
              if (r.models.length) {
                const typed = collect().models.filter((x) => r.models.includes(x));
                models.value = [...new Set([...r.models.slice(0, 8), ...typed])].join(", ");
                id = await save();
                toast(`已自动获取最新模型：${r.models.slice(0, 3).join("、")}…`, "ok");
              }
            } catch { /* some providers don't expose /models; keep the preset list */ }
          }
          if (!providerReady(store.settings.active?.provider, store.settings.providers?.[store.settings.active?.provider])) {
            await setActive(id, store.settings.providers[id].models[0]);
          }
          m.close();
          toast("已保存", "ok");
          navigate("#/settings/models");
        } catch (e) { toast(e.message, "err"); }
      } }, "保存")],
  });
}

// ---------------------------------------------------------------------------
// Voice (ASR + TTS)

function renderVoice(body) {
  body.innerHTML = "";
  const s = store.settings;
  const asr = s.asr || {};
  const tts = s.tts || {};
  const volc = tts.volc || {};
  const clone = tts.clone || {};
  const feats = store.meta?.features || {};
  const personas = store.meta?.personas || {};
  const provs = Object.entries(s.providers || {}).filter(([, p]) => p.type !== "chatgpt" && p.type !== "anthropic");
  const provSel = (val) => h("select", { class: "select" }, h("option", { value: "" }, "（选择已配置的提供方）"),
    provs.map(([pid, p]) => h("option", { value: pid, selected: pid === val }, p.name || pid)));

  // ---------------- TTS
  const ENGINES = [
    ["auto", "自动（按顺序尝试已配置的引擎）"], ["edge", "Edge 神经语音（免费）"], ["volcengine", "火山引擎 · 豆包语音（剪映/抖音同款）"],
    ["gptsovits", "声音克隆 · GPT-SoVITS（本地免费）"], ["api", "OpenAI 兼容语音 API"], ["local", "离线神经配音 · Kokoro（免费，本机运行）"],
    ["system", "系统自带语音（离线，效果差）"],
  ];
  const ttsEngine = h("select", { class: "select" }, ENGINES.map(([v, l]) => h("option", { value: v, selected: (tts.engine || "auto") === v }, l)));
  let persona = tts.persona || "解说男声";
  const player = h("audio", { controls: true, style: { width: "100%", display: "none", marginTop: "10px" } });
  const status = h("div", { class: "small muted", style: { marginTop: "6px" } });
  const preview = async (opts, btn) => {
    const old = btn?.innerHTML;
    if (btn) { btn.disabled = true; btn.innerHTML = '<div class="spinner"></div>'; }
    status.textContent = "合成中…";
    try {
      await saveTTS(true);
      const r = await post("/api/tts/preview", opts);
      player.src = rawUrl(r.path);
      player.style.display = "block";
      player.play();
      status.innerHTML = "";
      status.append(`当前使用：${r.engine}`, r.errors.length ? h("div", { style: { color: "var(--warn)" } }, "前面的引擎失败了：" + r.errors.join("；")) : "");
    } catch (e) { status.textContent = ""; toast(e.message, "err"); }
    if (btn) { btn.disabled = false; btn.innerHTML = old; }
  };
  const grid = h("div", { class: "suggest", style: { gridTemplateColumns: "repeat(auto-fill, minmax(210px, 1fr))" } });
  const renderGrid = () => {
    grid.innerHTML = "";
    for (const [name, pv] of Object.entries(personas)) {
      const sel = name === persona;
      grid.append(h("div", { class: "s", style: sel ? { borderColor: "var(--accent)", background: "var(--accent-soft)" } : {},
        onclick: () => { persona = name; renderGrid(); } },
        h("button", { class: "btn sm icon-only", title: "试听", html: icon("play"),
          onclick: (e) => { e.stopPropagation(); preview({ engine: ttsEngine.value === "auto" ? null : ttsEngine.value, voice: name }, e.currentTarget); } }),
        h("div", {}, h("b", {}, name, sel ? " ✓" : ""), h("span", {}, pv.desc))));
    }
  };
  renderGrid();
  const proxy = h("input", { class: "input mono", value: tts.proxy || "", placeholder: "例如 http://127.0.0.1:7890（留空则直连）" });
  const volcAppid = h("input", { class: "input mono", value: volc.appid || "", placeholder: "AppID" });
  const volcToken = h("input", { class: "input mono", type: "password", autocomplete: "off",
    placeholder: volc.token_set ? `已保存（${volc.token_hint}），留空则不修改` : "Access Token" });
  const volcVoice = h("input", { class: "input mono", value: volc.voice_type || "", placeholder: "音色 voice_type，从控制台音色列表复制" });
  const cloneUrl = h("input", { class: "input mono", value: clone.base_url || "http://127.0.0.1:9880" });
  const cloneRef = h("input", { class: "input mono", value: clone.ref_audio || "", placeholder: "参考音频的完整路径（5~10 秒、干净的人声）" });
  const clonePrompt = h("input", { class: "input", value: clone.prompt_text || "", placeholder: "参考音频里说的原话（一字不差）" });
  const refUpload = h("input", { type: "file", accept: "audio/*,video/*", style: { display: "none" } });
  refUpload.onchange = async () => {
    if (!refUpload.files.length) return;
    try { const [f] = await upload(refUpload.files); cloneRef.value = f.path; toast("参考音频已上传", "ok"); } catch (e) { toast(e.message, "err"); }
  };
  const ttsProv = provSel(tts.api_provider);
  const ttsModel = h("input", { class: "input", value: tts.api_model || "gpt-4o-mini-tts" });
  const ttsVoice = h("input", { class: "input", value: tts.api_voice || "alloy" });

  function localSection() {
    const box = h("div", { class: "card" });
    const render = (st) => {
      box.innerHTML = "";
      const tag = !st.available ? h("span", { class: "tag err" }, "缺少组件")
        : st.installed ? h("span", { class: "tag ok" }, "已就绪") : st.downloading ? h("span", { class: "tag accent" }, `下载中 ${Math.round((st.progress || 0) * 100)}%`)
          : h("span", { class: "tag" }, "未下载");
      box.append(h("div", { class: "row" }, h("b", {}, "离线神经配音（Kokoro）"), tag),
        h("div", { class: "small muted", style: { margin: "6px 0 10px" } },
          "在本机运行的中英文神经网络配音：免费、不联网、没有地区限制，55 个中文女声 + 45 个中文男声。",
          "Edge 配音用不了时会自动改用它；第一次使用需要下载约 150MB 的模型（国内会自动走镜像）。"),
        st.error ? h("div", { class: "notice err", html: icon("alert") }, h("div", {}, st.error)) : null,
        st.available && !st.installed && !st.downloading ? h("button", { class: "btn sm primary", html: icon("download") + "现在下载模型", onclick: async () => {
          try { render(await post("/api/tts/local/download")); poll(); } catch (e) { toast(e.message, "err"); }
        } }) : null);
    };
    const poll = () => setTimeout(async () => {
      const st = await get("/api/tts/local").catch(() => null);
      if (!st || !box.isConnected) return;
      render(st);
      if (st.downloading) poll(); else if (st.installed) toast("离线配音模型已就绪", "ok");
    }, 1500);
    get("/api/tts/local").then((st) => { render(st); if (st.downloading) poll(); }).catch(() => {});
    return box;
  }

  async function saveTTS(quiet = false) {
    const volcPatch = { appid: volcAppid.value.trim(), voice_type: volcVoice.value.trim() };
    if (volcToken.value.trim()) volcPatch.token = volcToken.value.trim();
    await put("/api/settings", { tts: {
      engine: ttsEngine.value, persona, proxy: proxy.value.trim(), volc: volcPatch,
      clone: { base_url: cloneUrl.value.trim(), ref_audio: cloneRef.value.trim(), prompt_text: clonePrompt.value.trim() },
      api_provider: ttsProv.value, api_model: ttsModel.value.trim(), api_voice: ttsVoice.value.trim(),
    } });
    await refreshSettings();
    if (!quiet) toast("配音设置已保存", "ok");
  }

  const section = (title, desc, ...children) => h("details", { class: "card", style: { padding: "14px 18px" } },
    h("summary", { style: { cursor: "pointer", fontWeight: 650 } }, title, h("span", { class: "small muted", style: { fontWeight: 400, marginLeft: "8px" } }, desc)),
    h("div", { style: { marginTop: "14px" } }, children));

  body.append(
    h("div", { class: "card" },
      h("h3", {}, "AI 配音"),
      h("p", { class: "small muted" }, "Agent 调用「AI 配音」时使用，会同时生成对齐的字幕。点 ▶ 试听，点卡片选为默认音色。"),
      h("div", { class: "field" }, h("label", {}, "引擎"), ttsEngine),
      grid, player, status,
      h("div", { class: "row", style: { marginTop: "14px" } }, h("div", { class: "grow" }),
        h("button", { class: "btn sm", html: icon("play") + "用当前设置试听", onclick: (e) => preview({ voice: persona }, e.currentTarget) }),
        h("button", { class: "btn sm primary", onclick: () => saveTTS() }, "保存"))),
    localSection(),
    section("Edge 神经语音", "免费；微软 2026-10 起在部分地区拒绝访问，可通过代理使用",
      h("div", { class: "field" }, h("label", {}, "代理"), proxy,
        h("div", { class: "hint" }, "如果试听报 403：在这里填写你的代理地址（Clash 默认 http://127.0.0.1:7890），并选择北美节点。"))),
    section("火山引擎 · 豆包语音", "剪映/抖音同款音色，新用户有试用额度",
      h("div", { class: "row" }, h("div", { class: "field grow" }, h("label", {}, "AppID"), volcAppid),
        h("div", { class: "field grow" }, h("label", {}, "Access Token"), volcToken)),
      h("div", { class: "field" }, h("label", {}, "音色 voice_type"), volcVoice),
      h("div", { class: "hint" }, "开通：火山引擎控制台 → 豆包语音 → 语音合成 → 创建应用，得到 AppID 和 Access Token；音色 ID 在 ",
        h("a", { href: "https://www.volcengine.com/docs/6561/97465", target: "_blank", rel: "noopener" }, "音色列表"), " 中复制（如“解说”“影视”类男声）。"),
      h("div", { class: "row", style: { marginTop: "10px" } }, h("div", { class: "grow" }),
        h("button", { class: "btn sm", html: icon("play") + "试听火山音色", onclick: (e) => preview({ engine: "volcengine" }, e.currentTarget) }))),
    section("声音克隆 · GPT-SoVITS", "上传几秒参考音频，克隆任意声音（本地运行，免费）",
      h("div", { class: "field" }, h("label", {}, "服务地址"), cloneUrl,
        h("div", { class: "hint" }, "先下载 GPT-SoVITS 整合包并运行 api_v2.py（默认端口 9880）。社区有大量现成的热门角色/解说音色模型。")),
      h("div", { class: "field" }, h("label", {}, "参考音频"), h("div", { class: "row" }, cloneRef,
        h("button", { class: "btn sm", html: icon("upload") + "上传", onclick: () => refUpload.click() }), refUpload)),
      h("div", { class: "field" }, h("label", {}, "参考音频文本"), clonePrompt),
      h("div", { class: "hint" }, "⚠️ 只克隆你自己或已获授权的声音，不要冒充真人。"),
      h("div", { class: "row", style: { marginTop: "10px" } }, h("div", { class: "grow" }),
        h("button", { class: "btn sm", html: icon("play") + "试听克隆声音", onclick: (e) => preview({ engine: "gptsovits" }, e.currentTarget) }))),
    section("OpenAI 兼容语音 API", "OpenAI TTS、硅基流动 CosyVoice、本地 CosyVoice/IndexTTS/Kokoro 等",
      h("div", { class: "row" }, h("div", { class: "field grow" }, h("label", {}, "提供方"), ttsProv),
        h("div", { class: "field grow" }, h("label", {}, "模型"), ttsModel), h("div", { class: "field grow" }, h("label", {}, "音色"), ttsVoice)),
      h("div", { class: "row" }, h("div", { class: "grow" }),
        h("button", { class: "btn sm", html: icon("play") + "试听 API 音色", onclick: (e) => preview({ engine: "api" }, e.currentTarget) }))),
  );

  // ---------------- ASR
  const engine = h("select", { class: "select" },
    h("option", { value: "local", selected: asr.engine !== "api" }, "本地 faster-whisper（免费、离线、隐私）"),
    h("option", { value: "api", selected: asr.engine === "api" }, "云端 API（OpenAI / Groq 等兼容 /audio/transcriptions）"));
  const model = h("select", { class: "select" }, [
    ["tiny", "tiny · 75MB · 最快，准确度低"], ["base", "base · 145MB"], ["small", "small · 480MB · 推荐 CPU 使用"],
    ["medium", "medium · 1.5GB"], ["large-v3-turbo", "large-v3-turbo · 1.6GB · 推荐 GPU，又快又准"], ["large-v3", "large-v3 · 3GB · 最准"],
  ].map(([v, l]) => h("option", { value: v, selected: (asr.model || "small") === v }, l)));
  const device = h("select", { class: "select" }, [["auto", "自动（有 NVIDIA 显卡则用 GPU）"], ["cpu", "CPU"], ["cuda", "GPU (CUDA)"]]
    .map(([v, l]) => h("option", { value: v, selected: (asr.device || "auto") === v }, l)));
  const mirror = h("input", { type: "checkbox", checked: !!asr.hf_endpoint });
  const apiProv = provSel(asr.api_provider);
  const apiModel = h("input", { class: "input", value: asr.api_model || "whisper-1" });
  const localBox = h("div", {}, h("div", { class: "row" }, h("div", { class: "field grow" }, h("label", {}, "模型大小"), model),
    h("div", { class: "field grow" }, h("label", {}, "设备"), device)),
  h("label", { class: "row small", style: { gap: "8px", cursor: "pointer" } }, mirror, "使用国内镜像下载模型（hf-mirror.com，中国大陆推荐）"),
  feats.faster_whisper ? null : h("div", { class: "notice err", style: { marginTop: "10px" } }, "未安装 faster-whisper：pip install faster-whisper"));
  const apiBox = h("div", {}, h("div", { class: "row" }, h("div", { class: "field grow" }, h("label", {}, "提供方"), apiProv),
    h("div", { class: "field grow" }, h("label", {}, "模型"), apiModel)),
  h("div", { class: "hint" }, "例：OpenAI 用 whisper-1；Groq 用 whisper-large-v3-turbo（Base URL https://api.groq.com/openai/v1）。"));
  const sync = () => { localBox.style.display = engine.value === "local" ? "" : "none"; apiBox.style.display = engine.value === "api" ? "" : "none"; };
  engine.onchange = sync;
  sync();
  body.append(h("div", { class: "card" }, h("h3", {}, "语音识别（生成字幕 / 理解视频台词）"),
    h("p", { class: "small muted" }, "Agent 调用「语音识别」和「看懂视频」时使用。"),
    h("div", { class: "field" }, h("label", {}, "识别引擎"), engine), localBox, apiBox,
    h("div", { class: "row", style: { marginTop: "10px" } }, h("div", { class: "grow" }), h("button", { class: "btn sm primary", onclick: async () => {
      await put("/api/settings", { asr: { engine: engine.value, model: model.value, device: device.value,
        hf_endpoint: mirror.checked ? "https://hf-mirror.com" : "", api_provider: apiProv.value, api_model: apiModel.value.trim() } });
      await refreshSettings();
      toast("语音识别设置已保存", "ok");
    } }, "保存"))));
}

// ---------------------------------------------------------------------------
// General

// ---------------------------------------------------------------------------
// Asset libraries (stock footage / images / music / sound effects)

const ASSET_KIND = { video: "视频画面", image: "图片", music: "音乐", sfx: "音效" };

function renderAssets(body) {
  body.innerHTML = "";
  const a = store.settings.assets || {};
  const provs = store.meta?.asset_providers || [];
  body.append(h("p", { class: "muted", style: { marginTop: 0 } },
    "Agent 剪视频时会自己搜索并下载素材：实拍画面、图片、背景音乐、音效，下载到工作区 assets/，并自动记录授权和署名。",
    "不填任何 key 也能用（Openverse、Wikimedia）；免费申请下面的 key 后，素材多得多、质量也更高，每个只要一两分钟。"));

  const test = async (btn, id) => {
    btn.disabled = true; btn.innerHTML = '<div class="spinner"></div>测试中';
    try {
      const r = await post("/api/assets/test", { provider: id });
      toast(r.count ? `可用，示例：${r.samples.join("、")}` : "可以连接，但这次没有搜到结果", "ok");
    } catch (e) { toast(e.message, "err"); }
    btn.disabled = false; btn.innerHTML = icon("zap") + "测试";
  };

  for (const p of provs) {
    const kinds = h("span", {}, p.kinds.map((k) => h("span", { class: "tag", style: { marginLeft: "4px" } }, ASSET_KIND[k])));
    const testBtn = h("button", { class: "btn sm", html: icon("zap") + "测试" });
    testBtn.onclick = () => test(testBtn, p.id);
    const card = h("div", { class: "card" });
    const head = h("div", { class: "row wrap", style: { gap: "6px" } }, h("b", {}, p.label), kinds, h("div", { class: "grow" }),
      p.key ? (a[`${p.key}_set`] ? h("span", { class: "tag ok" }, "已设置") : h("span", { class: "tag" }, "未设置"))
        : h("span", { class: "tag ok" }, "免 key，已启用"));
    card.append(head, h("div", { class: "small muted", style: { margin: "4px 0 10px" } }, p.desc));
    if (p.key) {
      const input = h("input", { class: "input mono", type: "password", placeholder: a[`${p.key}_set`] ? `已保存 ${a[`${p.key}_hint`] || ""}（要更换就粘贴新的）` : "粘贴 API key" });
      const save = h("button", { class: "btn sm primary", onclick: async () => {
        try {
          await put("/api/settings", { assets: { [p.key]: input.value.trim() } });
          await refreshSettings();
          toast(input.value.trim() ? "已保存，正在测试…" : "已清除", "ok");
          if (input.value.trim()) await test(testBtn, p.id);
          renderAssets(body);
        } catch (e) { toast(e.message, "err"); }
      } }, "保存");
      card.append(h("div", { class: "row", style: { gap: "8px" } }, input, save, a[`${p.key}_set`] ? testBtn : null),
        h("div", { class: "hint", style: { marginTop: "6px" } },
          h("a", { href: p.signup, target: "_blank", rel: "noopener" }, `免费申请 ${p.label} API key →`)));
    } else {
      card.append(h("div", { class: "row" }, testBtn));
    }
    body.append(card);
  }

  const commercial = h("input", { type: "checkbox", checked: a.commercial_only !== false });
  const proxy = h("input", { class: "input mono", value: a.proxy || "", placeholder: "留空 = 自动使用系统代理，例如 http://127.0.0.1:7890" });
  body.append(h("div", { class: "section-title" }, "选项"), h("div", { class: "card" },
    h("label", { class: "row small", style: { gap: "10px", cursor: "pointer", marginBottom: "12px" } },
      h("label", { class: "switch" }, commercial, h("span")),
      h("div", {}, h("b", {}, "只搜索可商用的素材（推荐）"),
        h("div", { class: "muted" }, "排除「仅限非商业」「禁止修改」的授权。做要发布、接广告的视频时请保持开启。"))),
    h("div", { class: "field" }, h("label", {}, "下载素材使用的代理"), proxy),
    h("div", { class: "row" }, h("div", { class: "grow" }), h("button", { class: "btn primary", onclick: async () => {
      await put("/api/settings", { assets: { commercial_only: commercial.checked, proxy: proxy.value.trim() } });
      await refreshSettings();
      toast("已保存", "ok");
    } }, "保存"))));
}

async function renderGeneral(body) {
  body.innerHTML = "";
  const s = store.settings;
  const ff = store.meta?.ffmpeg || {};
  const ws = h("input", { class: "input mono", value: s.workspace });
  const code = h("select", { class: "select" }, [["ask", "每个对话第一次询问（推荐）"], ["allow", "总是允许"], ["deny", "禁止"]]
    .map(([v, l]) => h("option", { value: v, selected: (s.permissions?.code || "ask") === v }, l)));
  const shell = h("select", { class: "select" }, [["ask", "每次询问（推荐）"], ["allow", "总是允许"], ["deny", "禁止"]]
    .map(([v, l]) => h("option", { value: v, selected: (s.permissions?.shell || "ask") === v }, l)));
  const steps = h("input", { class: "input", type: "number", min: 5, max: 300, value: s.agent?.max_steps || 150, style: { width: "120px" } });
  const extra = h("textarea", { class: "textarea", rows: 4, placeholder: "例如：我的频道叫「XX 科技」，字幕统一用黄色描边字；回复尽量简短。" });
  extra.value = s.agent?.extra_prompt || "";
  const ffpath = h("input", { class: "input mono", value: s.ffmpeg_path || "", placeholder: "留空自动查找（系统 PATH 或内置 ffmpeg）" });
  const mem = h("textarea", { class: "textarea", rows: 6, placeholder: "Agent 记住的偏好会出现在这里，也可以手动编辑。" });
  get("/api/memory").then((r) => { mem.value = r.content || ""; });
  const theme = h("select", { class: "select", style: { width: "160px" } }, [["dark", "深色"], ["light", "浅色"], ["system", "跟随系统"]]
    .map(([v, l]) => h("option", { value: v, selected: (localStorage.getItem("theme") || "dark") === v }, l)));
  theme.onchange = () => { localStorage.setItem("theme", theme.value); emit("theme"); };

  body.append(
    h("div", { class: "card" },
      h("div", { class: "field" }, h("label", {}, "工作区文件夹"), ws, h("div", { class: "hint" }, "上传的素材和生成的文件都保存在这里。Agent 的相对路径以此为根目录。")),
      h("div", { class: "row" }, h("div", { class: "field grow" }, h("label", {}, "执行命令行的权限"), shell),
        h("div", { class: "field grow" }, h("label", {}, "Agent 写代码处理画面 / 声音"), code),
        h("div", { class: "field" }, h("label", {}, "单次最多步数"), steps)),
      h("div", { class: "field" }, h("label", {}, "自定义指令（附加到系统提示词）"), extra),
      h("div", { class: "field" }, h("label", {}, "FFmpeg 路径"), ffpath,
        h("div", { class: "hint" }, ff.ffmpeg ? `当前：${ff.version || ff.ffmpeg}` : `未找到 ffmpeg：${ff.error || ""}`,
          ff.ffmpeg ? ` · libass ${ff.libass ? "✓" : "✗"} · drawtext ${ff.drawtext ? "✓" : "✗"} · x264 ${ff.libx264 ? "✓" : "✗"}` : "")),
      h("div", { class: "row" }, h("span", { class: "small muted" }, "界面主题"), theme, h("div", { class: "grow" }),
        h("button", { class: "btn primary", onclick: async () => {
          try {
            await put("/api/settings", { workspace: ws.value.trim(), permissions: { shell: shell.value, code: code.value }, ffmpeg_path: ffpath.value.trim(),
              agent: { max_steps: parseInt(steps.value, 10) || 150, extra_prompt: extra.value } });
            await refreshSettings();
            store.meta = await get("/api/meta");
            toast("已保存", "ok");
          } catch (e) { toast(e.message, "err"); }
        } }, "保存"))),
    h("div", { class: "card" }, h("h3", {}, "长期记忆"), h("p", { class: "small muted" }, "Agent 会在每次对话中参考这些内容（你的偏好、频道风格、常用素材位置等）。"),
      mem, h("div", { class: "row", style: { marginTop: "10px" } }, h("div", { class: "grow" }),
        h("button", { class: "btn sm primary", onclick: async () => { await put("/api/memory", { content: mem.value }); toast("记忆已保存", "ok"); } }, "保存记忆"))),
    h("div", { class: "card" }, h("h3", {}, "清理"), h("div", { class: "row" },
      h("span", { class: "small muted grow" }, "删除工作区中的临时文件（.tmp、.frames）。"),
      h("button", { class: "btn sm", onclick: async () => { const r = await post("/api/workspace/clean-tmp"); toast(`已释放 ${r.freed_mb} MB`, "ok"); } }, "清理临时文件"))));
}

// ---------------------------------------------------------------------------
// Tools & About

function renderTools(body) {
  body.innerHTML = "";
  const tools = store.meta?.tools || [];
  const groups = {};
  for (const t of tools) (groups[t.category_label] ||= []).push(t);
  body.append(h("p", { class: "muted", style: { marginTop: 0 } }, `Agent 当前可以使用 ${tools.length} 个工具。直接用自然语言描述需求即可，Agent 会自己选择和组合工具。`));
  for (const [label, list] of Object.entries(groups)) {
    body.append(h("div", { class: "section-title" }, `${label}（${list.length}）`),
      h("div", { class: "tool-list" }, list.map((t) => h("div", { class: "t" }, h("b", {}, t.name), t.dangerous ? h("span", { class: "tag warn", style: { marginLeft: "6px" } }, "需批准") : null,
        h("p", { title: t.description }, t.description)))));
  }
  const fx = store.meta?.effects || [];
  const tr = store.meta?.transitions || [];
  body.append(h("div", { class: "section-title" }, `特效（${fx.length}）`), h("div", { class: "row wrap", style: { gap: "6px" } }, fx.map((x) => h("span", { class: "tag" }, x))),
    h("div", { class: "section-title" }, `转场（${tr.length}）`), h("div", { class: "row wrap", style: { gap: "6px" } }, tr.map((x) => h("span", { class: "tag" }, x))));
}

function renderAbout(body) {
  const m = store.meta || {};
  const f = m.features || {};
  const ok = (v) => (v ? h("span", { class: "tag ok" }, "已安装") : h("span", { class: "tag warn" }, "未安装"));
  body.innerHTML = "";
  body.append(h("div", { class: "card" },
    h("div", { class: "row", style: { marginBottom: "14px" } }, h("div", { class: "brand-logo", html: icon("play") }),
      h("div", {}, h("h3", {}, `AutoStudio v${m.version}`), h("div", { class: "small muted" }, "开源 AI 视频创作 Agent · MIT License"))),
    h("div", { class: "kv" },
      h("div", {}, "系统"), h("div", {}, m.os),
      h("div", {}, "工作区"), h("div", { class: "mono" }, m.workspace),
      h("div", {}, "FFmpeg"), h("div", { class: "mono small" }, m.ffmpeg?.version || "未找到"),
      h("div", {}, "语音识别"), h("div", {}, ok(f.faster_whisper), h("span", { class: "small muted" }, "  faster-whisper")),
      h("div", {}, "AI 配音"), h("div", {}, ok(f.edge_tts), h("span", { class: "small muted" }, "  edge-tts")),
      h("div", {}, "视频下载"), h("div", {}, ok(f.yt_dlp), h("span", { class: "small muted" }, "  yt-dlp")))),
  h("div", { class: "card small muted" }, "安装全部可选功能：", h("code", { class: "mono" }, ' pip install "autostudio[all]"')));
}
