// First run: one guided pass (about five minutes) that leaves AutoStudio making a 小红书 video from each day's
// new material by itself, and asking on the phone before anything is posted. Opened automatically the first
// time on the PC; 设置 → 通用 can open it again. What was entered is kept in localStorage until it is done.
import { get, post, put } from "./api.js";
import { h, icon, toast } from "./dom.js";
import { anyProviderReady, modelLabel, navigate, providerName, refreshSettings, store } from "./store.js";

const DRAFT = "setup-draft";
const STEPS = ["开始", "连接 AI", "介绍一下", "小红书", "素材和时间", "手机", "完成"];
let poll = 0;

export function stopSetupView() {
  clearInterval(poll);
  poll = 0;
}

function loadDraft() {
  try { return JSON.parse(localStorage.getItem(DRAFT) || "{}") || {}; } catch { return {}; }
}

function saveDraft(d) {
  try { localStorage.setItem(DRAFT, JSON.stringify(d)); } catch { /* private mode */ }
}

export async function showSetup(main) {
  stopSetupView();
  const d = { step: 0, about: "", folder: "", time: "20:00", ask: true, accounts: null, autostart: true, shortcuts: true, code: true, ...loadDraft() };
  let info = await get("/api/setup");
  if (!d.about && info.about) d.about = info.about;
  if (!d.folder) d.folder = info.task?.watch_dir || info.default_folder;
  if (info.task) {
    const [mi, hr] = info.task.cron.split(" ");
    if (/^\d+$/.test(mi) && /^\d+$/.test(hr) && !loadDraft().time) d.time = `${hr.padStart(2, "0")}:${mi.padStart(2, "0")}`;
    if (!("ask" in loadDraft())) d.ask = !info.task.auto_publish;
  }
  const card = h("div", { class: "card setup-card" });
  const dots = h("div", { class: "setup-dots" });
  main.innerHTML = "";
  main.append(h("div", { class: "page-body setup-page" }, h("div", { class: "setup-wrap" },
    h("div", { class: "brand setup-brand" }, h("div", { class: "brand-logo", html: icon("play") }), "AutoStudio",
      h("div", { class: "grow" }),
      h("a", { class: "small muted", href: "javascript:void 0", onclick: skip }, info.task ? "退出向导" : "跳过，我自己来")),
    dots, card)));

  const go = (n) => { d.step = Math.max(0, Math.min(STEPS.length - 1, n)); saveDraft(d); render(); };
  const nav = (back, next, nextLabel = "下一步", opts = {}) => h("div", { class: "setup-nav" },
    back ? h("button", { class: "btn ghost", onclick: () => go(d.step - 1) }, "上一步") : h("span"),
    h("button", { class: `btn ${opts.quiet ? "" : "primary"}`, disabled: !!opts.disabled, onclick: next }, nextLabel));

  async function skip() {
    await post("/api/setup/skip").catch(() => {});
    localStorage.removeItem(DRAFT);
    store.meta.app = await get("/api/app");
    navigate("#/chat");
  }

  function render() {
    stopSetupView();
    dots.innerHTML = "";
    STEPS.forEach((s, i) => dots.append(h("div", { class: `setup-dot ${i < d.step ? "done" : i === d.step ? "on" : ""}`, title: s,
      onclick: () => i < d.step && go(i) }, h("i"), h("span", {}, s))));
    card.innerHTML = "";
    [welcome, model, about, xhs, schedule, phone, finish][d.step]();
  }

  // 0 ------------------------------------------------------------------------
  function welcome() {
    card.append(
      h("h2", {}, "每天自动做好小红书视频"),
      h("p", { class: "setup-lead" }, "以后每天你只做一件事：把拍的视频、照片放进「素材」文件夹。"),
      h("div", { class: "setup-flow" },
        flowItem("folder", "放素材", "手机拍的直接拷进文件夹"),
        flowItem("clock", "到点自动剪", "AI 剪辑、配字幕、做封面"),
        flowItem("phone", "手机叮一声", "看一眼封面和标题"),
        flowItem("check", "点「同意」", "自动发到小红书")),
      h("p", { class: "muted small" }, "下面跟着设置一次，大约 5 分钟。以后开机自动运行，不用再管。"),
      nav(false, () => go(1), "开始设置"));
  }

  function flowItem(ico, title, sub) {
    return h("div", { class: "setup-flow-item" }, h("div", { class: "setup-flow-ico", html: icon(ico) }), h("b", {}, title), h("span", {}, sub));
  }

  // 1 ------------------------------------------------------------------------
  function model() {
    const ready = anyProviderReady();
    const a = store.settings?.active || {};
    card.append(h("h2", {}, "连接 AI"),
      h("p", { class: "setup-lead" }, "剪视频、写标题文案的是 AI。用你的 ChatGPT 账号登录最简单（用的是订阅额度，不另外花钱）。"));
    if (ready) {
      card.append(h("div", { class: "notice", html: icon("check") }, h("div", {}, h("b", {}, "已连接："), `${providerName(a.provider)} · ${modelLabel(a.provider, a.model)}`)),
        nav(true, () => go(2)));
      return;
    }
    const status = h("div");
    const login = h("button", { class: "btn primary", html: icon("zap") + "登录 ChatGPT", onclick: async () => {
      let r;
      try { r = await post("/api/auth/chatgpt/login"); } catch (e) { return toast(e.message, "err"); }
      window.open(r.auth_url, "_blank", "noopener");
      status.innerHTML = "";
      status.append(h("div", { class: "notice" }, h("div", { class: "spinner" }), h("div", {}, h("b", {}, "请在新打开的页面里登录 ChatGPT"),
        h("div", { class: "small muted" }, "登录完会自动回到这里。"), h("a", { class: "small", href: r.auth_url, target: "_blank", rel: "noopener" }, "没有弹出？点这里"))));
      clearInterval(poll);
      poll = setInterval(async () => {
        const s = await get("/api/auth/chatgpt").catch(() => null);
        if (!s?.logged_in) return;
        clearInterval(poll);
        try { await post("/api/providers/chatgpt/models"); } catch { /* the built-in list will do */ }
        await refreshSettings();
        const models = store.settings.providers.chatgpt.models || [];
        await put("/api/settings", { active: { provider: "chatgpt", model: models[0] } });
        await refreshSettings();
        toast("ChatGPT 已连接", "ok");
        go(2);
      }, 2000);
    } });
    card.append(h("div", { class: "row wrap" }, login,
      h("button", { class: "btn", onclick: () => { saveDraft(d); navigate("#/settings/models"); } }, "我有 API Key")),
      status, nav(true, () => go(2), "先跳过", { quiet: true }));
  }

  // 2 ------------------------------------------------------------------------
  function about() {
    const ta = h("textarea", { class: "textarea", rows: 5, placeholder: "例如：我在温哥华做室内装修二十年，主要做厨房、卫生间翻新和全屋改造，做工细、讲究防水。想让附近要装修的华人看到我的活，愿意来找我。" });
    ta.value = d.about || "";
    ta.addEventListener("input", () => { d.about = ta.value; saveDraft(d); });
    card.append(h("h2", {}, "你是做什么的？"),
      h("p", { class: "setup-lead" }, "AI 会用你的身份和口吻来写标题、正文和话题。说得越具体，写出来越像你。"),
      h("div", { class: "field" }, ta, h("div", { class: "hint" }, "可以写：做什么、在哪个城市、做了多少年、拿手什么、想吸引什么样的客户。以后在「设置 → 通用 → 长期记忆」里随时能改。")),
      nav(true, () => go(3)));
    setTimeout(() => ta.focus(), 50);
  }

  // 3 ------------------------------------------------------------------------
  async function xhs() {
    card.append(h("h2", {}, "登录小红书"), h("p", { class: "setup-lead" }, "做好的视频发到这个账号。登录信息只保存在这台电脑上。"));
    const list = h("div", { class: "setup-accounts" });
    const status = h("div");
    card.append(list, status);
    const next = h("button", { class: "btn primary", onclick: () => go(4) }, "下一步");
    card.append(h("div", { class: "setup-nav" }, h("button", { class: "btn ghost", onclick: () => go(2) }, "上一步"), next));

    async function load() {
      const accs = (await get("/api/social/accounts")).filter((a) => a.platform === "xiaohongshu");
      if (d.accounts === null || !accs.some((a) => d.accounts.includes(a.id))) d.accounts = accs.map((a) => a.id);
      list.innerHTML = "";
      for (const a of accs) {
        const cb = h("input", { type: "checkbox", checked: d.accounts.includes(a.id), onchange: () => {
          d.accounts = cb.checked ? [...new Set([...d.accounts, a.id])] : d.accounts.filter((x) => x !== a.id);
          saveDraft(d);
        } });
        list.append(h("label", { class: "setup-account" }, cb,
          a.avatar ? h("img", { src: a.avatar, referrerpolicy: "no-referrer" }) : h("div", { class: "setup-account-noimg" }, "小红书"),
          h("div", { class: "grow" }, h("b", {}, a.name), h("div", { class: "small muted" }, a.status === "ok" ? "已登录" : "登录已失效，点下面重新登录"))));
      }
      list.append(h("button", { class: `btn ${accs.length ? "" : "primary"}`, html: icon("plus") + (accs.length ? "再登录一个账号" : "登录小红书"), onclick: login }));
      next.textContent = accs.length ? "下一步" : "先跳过（只做视频，不发布）";
      next.className = `btn ${accs.length ? "primary" : ""}`;
    }

    async function login() {
      let job;
      try { job = await post("/api/social/xiaohongshu/login", {}); } catch (e) { return toast(e.message, "err"); }
      status.innerHTML = "";
      const step = h("div", { class: "small muted" });
      status.append(h("div", { class: "notice" }, h("div", { class: "spinner" }), h("div", {},
        h("b", {}, "已经在电脑上打开了一个浏览器窗口"),
        h("div", { class: "small muted" }, "在窗口里用手机验证码登录（或点右上角的二维码图标，用小红书 App 扫码）。登录成功后窗口会自己关掉。"), step)));
      clearInterval(poll);
      poll = setInterval(async () => {
        const j = await get(`/api/social/jobs/${job.id}`).catch(() => null);
        if (!j) return;
        if (j.status === "running") { step.textContent = j.text || ""; return; }
        clearInterval(poll);
        status.innerHTML = "";
        if (j.status === "done") { toast(j.note || "小红书已登录", "ok"); d.accounts = null; load(); }
        else status.append(h("div", { class: "notice err", html: icon("alert") }, h("div", {}, j.status === "cancelled" ? "登录窗口关掉了，没有登录。" : `登录失败：${j.error}`)));
      }, 1500);
    }

    await load();
  }

  // 4 ------------------------------------------------------------------------
  function schedule() {
    const folder = h("input", { class: "input mono", value: d.folder });
    const time = h("input", { class: "input", type: "time", value: d.time, style: { width: "140px" } });
    const ask = h("input", { type: "radio", name: "ask", checked: d.ask });
    const direct = h("input", { type: "radio", name: "ask", checked: !d.ask });
    const sync = () => { d.folder = folder.value.trim(); d.time = time.value || "20:00"; d.ask = ask.checked; saveDraft(d); };
    [folder, time, ask, direct].forEach((el) => el.addEventListener("change", sync));
    card.append(h("h2", {}, "素材放哪，几点做"),
      h("div", { class: "field" }, h("label", {}, "素材文件夹"), folder,
        h("div", { class: "hint" }, "每天把拍的视频、照片放进这里。用过的会自动挪进里面的「已用过」，同一个视频不会被做第二次。")),
      h("div", { class: "field" }, h("label", {}, "每天几点开始做"), time,
        h("div", { class: "hint" }, "这个时间电脑要开着（睡眠也行，醒来后会补做）。剪一条一般要 10~40 分钟。")),
      h("div", { class: "field" }, h("label", {}, "做好以后"),
        h("label", { class: "setup-choice" }, ask, h("div", {}, h("b", {}, "先发到我手机，我点「同意」才发"), h("span", { class: "tag ok" }, "推荐"))),
        h("label", { class: "setup-choice" }, direct, h("div", {}, h("b", {}, "直接发布，不用问我")))),
      nav(true, () => { sync(); go(5); }));
  }

  // 5 ------------------------------------------------------------------------
  async function phone() {
    card.append(h("h2", {}, "连上你的手机"),
      h("p", { class: "setup-lead" }, "做好了手机会响一声，看一眼封面和标题，点「同意」就发出去。在外面用流量也能收到。"));
    const body = h("div");
    card.append(body, nav(true, () => go(6), "下一步"));
    let st;
    try {
      st = await get("/api/remote");
      if (!st.enabled) st = await put("/api/remote", { enabled: true });
    } catch (e) { body.append(h("div", { class: "notice err" }, e.message)); return; }
    const known = st.devices.length;
    const draw = () => {
      body.innerHTML = "";
      if (!st.running || !st.addresses.length) {
        body.append(h("div", { class: "notice err", html: icon("wifi") }, h("div", {}, st.error || "这台电脑现在没有连上网络，连上 Wi-Fi 后再试。")));
        return;
      }
      const code = st.pairing?.code || "------";
      body.append(h("div", { class: "setup-phone" },
        h("img", { class: "pair-qr", src: `/api/remote/qr.svg?c=${code}`, alt: "配对二维码" }),
        h("div", { class: "grow" },
          h("ol", { class: "setup-steps" },
            h("li", {}, "手机连上和这台电脑", h("b", {}, "同一个 Wi-Fi"), "。"),
            h("li", {}, "用手机相机扫左边的二维码，打开的页面里点「下载安卓 App」并安装。"),
            h("li", {}, "打开 AutoStudio App，点「扫码连接」再扫一次这个二维码。"),
            h("li", {}, "App 问能不能发通知时，点「允许」。")),
          h("div", { class: "small muted" }, "配对码 ", h("b", { class: "mono" }, `${code.slice(0, 3)} ${code.slice(3)}`), " · iPhone 用 Safari 扫码后可以添加到主屏幕（iPhone 收不到通知）。"),
          st.devices.length ? h("div", { class: "notice", html: icon("check") }, h("div", {}, `已连接：${st.devices.map((x) => x.name).join("、")}`)) : null)));
    };
    draw();
    // a dismissed "allow access" prompt in Windows silently keeps every phone out
    const fw = h("div");
    card.insertBefore(fw, card.lastChild);
    get("/api/remote/firewall").then((f) => {
      if (!f?.checked || f.ok) return;
      const fix = h("button", { class: "btn sm primary", html: icon("check") + "一键放行" });
      fix.onclick = async () => {
        fix.disabled = true;
        fix.innerHTML = '<div class="spinner"></div>在弹出的窗口里点「是」';
        try { await post("/api/remote/firewall/fix"); fw.innerHTML = ""; toast("已放行，手机现在可以连了", "ok"); }
        catch (e) { toast(e.message, "err"); fix.disabled = false; fix.innerHTML = icon("check") + "一键放行"; }
      };
      fw.append(h("div", { class: "notice err", html: icon("alert") }, h("div", {},
        "Windows 防火墙会挡住手机。点一下放行（只对家里的网络开放，需要在弹出的窗口里点「是」）。", h("div", { style: { marginTop: "8px" } }, fix))));
    }).catch(() => {});
    poll = setInterval(async () => {
      const next = await get("/api/remote").catch(() => null);
      if (!next) return;
      const changed = next.devices.length !== st.devices.length || next.pairing?.code !== st.pairing?.code;
      st = next;
      if (changed) draw();
      if (st.devices.length > known) toast(`「${st.devices[st.devices.length - 1].name}」已连接`, "ok");
    }, 2500);
  }

  // 6 ------------------------------------------------------------------------
  function finish() {
    const auto = h("input", { type: "checkbox", checked: d.autostart });
    const links = h("input", { type: "checkbox", checked: d.shortcuts });
    const code = h("input", { type: "checkbox", checked: d.code });
    const can = store.meta?.app?.autostart?.supported;
    const btn = h("button", { class: "btn primary", html: icon("check") + "完成设置" });
    card.append(h("h2", {}, "最后确认"),
      h("div", { class: "setup-summary" },
        line("folder", "素材文件夹", d.folder),
        line("clock", "每天", `${d.time} 开始做`),
        line("phone", "做好以后", d.ask ? "发到手机，你点「同意」才发" : "直接发布"),
        line("bot", "AI", anyProviderReady() ? "已连接" : "还没连接：做之前要先到「设置」里连上")),
      can ? h("label", { class: "setup-check" }, auto, h("div", {}, h("b", {}, "开机自动运行"), h("span", {}, "电脑开着就会按时做，不用记得打开 AutoStudio"))) : null,
      can ? h("label", { class: "setup-check" }, links, h("div", {}, h("b", {}, "在桌面放快捷方式"), h("span", {}, "「AutoStudio」和「素材」文件夹"))) : null,
      h("label", { class: "setup-check" }, code, h("div", {}, h("b", {}, "让 AI 自己写代码做特效"), h("span", {}, "效果更好；AI 会在这台电脑上运行它写的代码，无人看着时不再一一询问"))),
      h("div", { class: "setup-nav" }, h("button", { class: "btn ghost", onclick: () => go(5) }, "上一步"), btn));
    btn.onclick = async () => {
      btn.disabled = true;
      btn.innerHTML = '<div class="spinner"></div>正在设置…';
      d.autostart = auto.checked; d.shortcuts = links.checked; d.code = code.checked;
      try {
        const r = await post("/api/setup/finish", { about: d.about, folder: d.folder, time: d.time, auto_publish: !d.ask,
          accounts: d.accounts || [], autostart: d.autostart, shortcuts: d.shortcuts, allow_code: d.code });
        localStorage.removeItem(DRAFT);
        store.meta.app = await get("/api/app");
        done(r);
      } catch (e) {
        toast(e.message, "err");
        btn.disabled = false;
        btn.innerHTML = icon("check") + "完成设置";
      }
    };
  }

  function line(ico, k, v) {
    return h("div", { class: "setup-line" }, h("span", { html: icon(ico) }), h("span", { class: "muted" }, k), h("b", {}, v));
  }

  function done(r) {
    stopSetupView();
    dots.innerHTML = "";
    card.innerHTML = "";
    const t = r.task;
    const tryBtn = h("button", { class: "btn", html: icon("play") + "现在就做一次", onclick: async () => {
      try {
        const x = await post(`/api/tasks/${t.id}/run`);
        if (x.skipped) return toast("素材文件夹里还没有东西：先放几段视频进去再点", "err");
        navigate(`#/chat/${x.session_id}`);
      } catch (e) { toast(e.message, "err"); }
    } });
    card.append(h("div", { class: "setup-done-ico", html: icon("check") }),
      h("h2", { style: { textAlign: "center" } }, "全部设置好了"),
      h("p", { class: "setup-lead", style: { textAlign: "center" } },
        `以后每天往「${t.watch_dir.split(/[\\/]/).pop()}」文件夹里放素材就行，${d.time} 会自动开始做。`),
      h("div", { class: "setup-summary" },
        line("folder", "素材文件夹", t.watch_dir),
        line("clock", "下次运行", t.next_run || d.time),
        r.autostart_on ? line("zap", "开机", "自动运行") : null,
        (r.shortcuts || []).length ? line("monitor", "桌面", "已放好快捷方式") : null),
      h("div", { class: "row wrap", style: { justifyContent: "center", marginTop: "18px" } },
        h("button", { class: "btn", html: icon("folder") + "打开素材文件夹", onclick: () => post("/api/files/reveal", { path: t.watch_dir }) }),
        tryBtn,
        h("button", { class: "btn primary", onclick: () => navigate("#/tasks") }, "进入 AutoStudio")));
  }

  render();
}
