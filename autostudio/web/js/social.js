// 发布 page: social accounts (several 小红书 / YouTube accounts each), the review queue and publish history.
import { del, get, patch, post, put, rawUrl } from "./api.js";
import { basename, confirmDialog, fmtTime, h, icon, kindOf, modal, toast } from "./dom.js";
import { refreshSettings, store } from "./store.js";

const PLAT = {
  xiaohongshu: { label: "小红书", color: "#ff2442", logo: "小红书" },
  youtube: { label: "YouTube", color: "#ff0033", logo: "▶" },
};
const STATUS = { pending: ["待确认", "warn"], publishing: ["发布中", "accent"], published: ["已发布", "ok"], failed: ["失败", "err"], cancelled: ["已取消", ""] };
const GCP = {
  project: "https://console.cloud.google.com/projectcreate",
  api: "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
  consent: "https://console.cloud.google.com/auth/overview",
  audience: "https://console.cloud.google.com/auth/audience",
  clients: "https://console.cloud.google.com/auth/clients",
  audit: "https://support.google.com/youtube/contact/yt_api_form",
};

let timer = 0;
export function stopPublishView() { clearInterval(timer); }

function logo(platform, avatar) {
  const p = PLAT[platform] || { color: "#64748b", logo: "?" };
  const el = h("div", { class: "prov-logo", style: { background: p.color, fontSize: p.logo.length > 1 ? "11px" : "16px" } }, p.logo);
  if (avatar) {
    const img = h("img", { src: avatar, referrerpolicy: "no-referrer", class: "acc-avatar", onerror: () => img.replaceWith(el) });
    return h("div", { class: "acc-avatar-wrap" }, img, h("span", { class: "acc-badge", style: { background: p.color } }));
  }
  return el;
}

function thumb(c) {
  const f = c.video || (c.images || [])[0];
  if (!f) return h("div", { class: "post-thumb" });
  const url = rawUrl(f);
  return h("div", { class: "post-thumb" }, kindOf(f) === "video"
    ? h("video", { src: url + "#t=0.5", preload: "metadata", muted: true, onclick: () => window.open(url, "_blank") })
    : h("img", { src: url, loading: "lazy", onclick: () => window.open(url, "_blank") }),
  (c.images || []).length > 1 ? h("span", { class: "post-count" }, `${c.images.length} 张`) : null);
}

export async function showPublish(main) {
  stopPublishView();
  main.innerHTML = "";
  await refreshSettings();
  const accountsBox = h("div");
  const pendingBox = h("div");
  const historyBox = h("div");
  const settingsBox = h("div");
  main.append(
    h("div", { class: "page-head" },
      h("button", { class: "btn ghost icon-only menu-btn", html: icon("menu"), onclick: () => document.getElementById("sidebar").classList.toggle("open") }),
      h("h1", {}, "发布"), h("div", { class: "grow" }),
      h("button", { class: "btn sm", html: icon("plus") + "小红书账号", onclick: () => addXhs() }),
      h("button", { class: "btn sm primary", html: icon("plus") + "YouTube 账号", onclick: () => addYoutube() })),
    h("div", { class: "page-body" }, h("div", { class: "page-narrow" },
      h("p", { class: "muted", style: { marginTop: 0 } },
        "把 Agent 做好的视频一键发到小红书和 YouTube，每个平台都能添加多个账号。在对话里说「发到我的小红书和 YouTube」就行；",
        "定时任务产出的内容默认先放进「待确认」，你点一下才会发出。"),
      pendingBox, accountsBox, historyBox, settingsBox)));

  let accounts = [];
  let posts = [];

  async function loadAccounts() {
    try { accounts = await get("/api/social/accounts"); } catch (e) { return toast(e.message, "err"); }
    accountsBox.innerHTML = "";
    accountsBox.append(h("div", { class: "section-title" }, `账号（${accounts.length}）`));
    if (!accounts.length) {
      accountsBox.append(h("div", { class: "card empty" },
        h("div", { style: { marginBottom: "14px" } }, "还没有添加社交账号。小红书扫码登录即可；YouTube 需要先做一次 Google 授权设置（有分步引导）。"),
        h("div", { class: "row", style: { justifyContent: "center", gap: "10px" } },
          h("button", { class: "btn", html: icon("plus") + "添加小红书账号", onclick: () => addXhs() }),
          h("button", { class: "btn primary", html: icon("plus") + "添加 YouTube 账号", onclick: () => addYoutube() }))));
      return;
    }
    const grid = h("div", { class: "acc-grid" });
    for (const a of accounts) grid.append(accountCard(a));
    accountsBox.append(grid);
  }

  function accountCard(a) {
    const ok = a.status === "ok";
    const name = h("b", { class: "acc-name", title: "点击重命名", onclick: async () => {
      const n = prompt("账号显示名称（只在 AutoStudio 里使用）", a.name);
      if (n && n.trim()) { await patch(`/api/social/accounts/${a.id}`, { name: n.trim() }); loadAccounts(); }
    } }, a.name);
    const checkBtn = h("button", { class: "btn sm", html: icon("refresh") + "检查登录" });
    checkBtn.onclick = async () => {
      checkBtn.disabled = true; checkBtn.innerHTML = '<div class="spinner"></div>检查中';
      try {
        const r = await post(`/api/social/accounts/${a.id}/check`);
        toast(r.status === "ok" ? `「${r.name}」登录正常` : `「${r.name}」登录已失效，请重新登录`, r.status === "ok" ? "ok" : "err");
      } catch (e) { toast(e.message, "err"); }
      loadAccounts();
    };
    return h("div", { class: "card acc-card" },
      h("div", { class: "row", style: { gap: "12px", alignItems: "center" } }, logo(a.platform, a.avatar),
        h("div", { class: "grow", style: { minWidth: 0 } },
          h("div", { class: "row", style: { gap: "6px" } }, name,
            ok ? h("span", { class: "tag ok" }, "已登录") : h("span", { class: "tag err" }, "需要重新登录")),
          h("div", { class: "small muted" }, a.platform_label, a.handle ? ` · ${a.handle}` : "", ` · 添加于 ${fmtTime(a.created_at)}`))),
      h("div", { class: "row wrap", style: { marginTop: "12px", gap: "6px" } },
        ok ? checkBtn : h("button", { class: "btn sm primary", html: icon("refresh") + "重新登录",
          onclick: () => (a.platform === "youtube" ? addYoutube(a.id) : addXhs(a.id)) }),
        a.platform === "xiaohongshu" ? h("button", { class: "btn sm", html: icon("play") + "试运行", title: "上传一段测试视频并填好表单，但不点发布",
          onclick: () => testXhs(a) }) : null,
        h("div", { class: "grow" }),
        h("button", { class: "btn sm ghost danger icon-only", html: icon("trash"), title: "移除账号", onclick: async () => {
          if (!(await confirmDialog(`移除${a.platform_label}账号「${a.name}」？本机保存的登录信息会被删除，待确认的内容会被取消。`, { danger: true, okText: "移除" }))) return;
          await del(`/api/social/accounts/${a.id}`); loadAccounts(); loadPosts();
        } })));
  }

  async function loadPosts() {
    try { posts = await get("/api/social/posts?limit=80"); } catch (e) { return toast(e.message, "err"); }
    const pending = posts.filter((p) => p.status === "pending");
    const history = posts.filter((p) => p.status !== "pending");
    pendingBox.innerHTML = "";
    if (pending.length) {
      pendingBox.append(h("div", { class: "section-title" }, h("span", { class: "tag warn" }, pending.length), "等你确认的内容",
        h("div", { class: "grow" }),
        pending.length > 1 ? h("button", { class: "btn sm primary", html: icon("upload") + "全部发布", onclick: async () => {
          if (!(await confirmDialog(`把 ${pending.length} 条内容全部发布？`))) return;
          for (const p of pending) await post(`/api/social/posts/${p.id}/publish`).catch((e) => toast(e.message, "err"));
          loadPosts();
        } }) : null));
      for (const p of pending) pendingBox.append(postCard(p, true));
    }
    historyBox.innerHTML = "";
    historyBox.append(h("div", { class: "section-title" }, "发布记录"));
    if (!history.length) historyBox.append(h("div", { class: "card small muted" }, "还没有发布过内容。"));
    for (const p of history) historyBox.append(postCard(p, false));
  }

  function postCard(p, isPending) {
    const c = p.content || {};
    const [label, cls] = STATUS[p.status] || [p.status, ""];
    const live = p.status === "publishing" && p.live
      ? h("div", { class: "post-live" }, h("div", { class: "bar" }, h("i", { style: { width: `${Math.round((p.live.progress || 0) * 100)}%` } })),
        h("span", { class: "small muted" }, p.live.text || "")) : null;
    const actions = h("div", { class: "row wrap", style: { gap: "6px", marginTop: "10px" } });
    if (isPending) {
      actions.append(
        h("button", { class: "btn sm primary", html: icon("upload") + "发布", onclick: () => publishPost(p) }),
        h("button", { class: "btn sm", html: icon("edit") + "修改后发布", onclick: () => editPost(p) }),
        h("button", { class: "btn sm ghost", html: icon("x") + "不发了", onclick: async () => { await post(`/api/social/posts/${p.id}/cancel`); loadPosts(); } }));
    } else {
      if (p.url) actions.append(h("a", { class: "btn sm", href: p.url, target: "_blank", rel: "noopener", html: icon("external") + (p.platform === "youtube" ? "打开视频" : "笔记管理") }));
      if (p.status === "failed" || p.status === "cancelled") actions.append(h("button", { class: "btn sm", html: icon("refresh") + "重新发布", onclick: () => publishPost(p) }));
      if (p.status !== "publishing") actions.append(h("button", { class: "btn sm ghost icon-only", html: icon("trash"), title: "删除记录", onclick: async () => { await del(`/api/social/posts/${p.id}`); loadPosts(); } }));
    }
    if (p.session_id) actions.append(h("a", { class: "btn sm ghost", href: `#/chat/${p.session_id}`, html: icon("chat") + "来源对话" }));
    const shot = (p.note || "").includes("截图：") ? p.note.split("截图：")[1].trim() : "";
    return h("div", { class: "card post-card" }, thumb(c),
      h("div", { class: "grow", style: { minWidth: 0 } },
        h("div", { class: "row wrap", style: { gap: "6px" } }, h("span", { class: `tag ${cls}` }, label),
          h("span", { class: "small", style: { color: PLAT[p.platform]?.color } }, p.platform_label), h("span", { class: "small" }, p.account_name),
          h("span", { class: "small muted" }, fmtTime(p.updated_at)),
          c.privacy && c.privacy !== "public" ? h("span", { class: "tag" }, c.privacy === "unlisted" ? "不公开列出" : "私享") : null),
        h("div", { class: "post-title" }, c.title || basename(c.video || "")),
        c.description ? h("div", { class: "post-desc" }, c.description) : null,
        (c.tags || []).length ? h("div", { class: "small", style: { color: "var(--accent-2)", marginTop: "4px" } }, c.tags.map((t) => `#${t}`).join(" ")) : null,
        live,
        p.error ? h("div", { class: "notice err", html: icon("alert") }, h("div", {}, p.error)) : null,
        p.note && !shot ? h("div", { class: "notice", html: icon("info") }, h("div", {}, p.note)) : null,
        shot ? h("a", { class: "small", href: rawUrl(shot), target: "_blank" }, "查看出错时的页面截图") : null,
        actions));
  }

  async function publishPost(p, edits = {}) {
    try { await post(`/api/social/posts/${p.id}/publish`, edits); toast("开始发布", "ok"); } catch (e) { toast(e.message, "err"); }
    loadPosts();
  }

  function editPost(p) {
    const c = p.content || {};
    const max = p.platform === "xiaohongshu" ? 20 : 100;
    const title = h("input", { class: "input", value: c.title || "", maxlength: max });
    const desc = h("textarea", { class: "textarea", rows: 7 });
    desc.value = c.description || "";
    const tags = h("input", { class: "input", value: (c.tags || []).join(" "), placeholder: "用空格分隔，不用写 #" });
    const m = modal({
      title: `修改后发布到 ${p.platform_label}「${p.account_name}」`,
      body: h("div", {},
        h("div", { class: "field" }, h("label", {}, `标题（≤${max} 字）`), title),
        h("div", { class: "field" }, h("label", {}, p.platform === "youtube" ? "简介" : "正文"), desc),
        h("div", { class: "field" }, h("label", {}, "话题标签"), tags)),
      footer: [h("button", { class: "btn", onclick: () => m.close() }, "取消"),
        h("button", { class: "btn primary", onclick: async () => {
          m.close();
          await publishPost(p, { title: title.value.trim(), description: desc.value, tags: tags.value.split(/[\s,，#]+/).filter(Boolean) });
        } }, "发布")],
    });
  }

  // ---- 小红书: login in a real browser window, one profile per account
  async function addXhs(accountId) {
    let job;
    try { job = await post("/api/social/xiaohongshu/login", { account_id: accountId || null }); } catch (e) { return toast(e.message, "err"); }
    const status = h("div", { class: "notice" }, h("div", { class: "spinner" }), h("div", {},
      h("b", {}, "已在你的电脑上打开一个浏览器窗口"),
      h("div", { class: "small muted" }, "请在窗口里用小红书 App 扫码（或手机验证码）登录「小红书创作服务平台」。登录成功后窗口会自动关闭。")));
    const m = modal({
      title: accountId ? "重新登录小红书" : "添加小红书账号",
      body: h("div", {}, status, h("div", { class: "hint", style: { marginTop: "10px" } },
        "每个账号使用一个独立的浏览器配置，互不干扰，登录信息只保存在你的电脑上。要添加多个账号，就重复这个步骤，每次登录不同的小红书号。")),
      footer: [h("button", { class: "btn", onclick: () => m.close() }, "关闭")],
      onClose: () => clearInterval(poll),
    });
    const poll = setInterval(async () => {
      const j = await get(`/api/social/jobs/${job.id}`).catch(() => null);
      if (!j || j.status === "running") return;
      clearInterval(poll);
      if (j.status === "done") { m.close(); toast(j.note || "小红书账号已添加", "ok"); loadAccounts(); return; }
      status.className = "notice err";
      status.innerHTML = icon("alert");
      status.append(h("div", {}, j.status === "cancelled" ? "登录窗口已关闭，没有添加账号。" : `登录失败：${j.error}`));
    }, 1500);
  }

  async function testXhs(a) {
    let job;
    try { job = await post(`/api/social/xiaohongshu/${a.id}/test`); } catch (e) { return toast(e.message, "err"); }
    const text = h("span", {}, "正在打开浏览器…");
    const box = h("div", {}, h("div", { class: "notice" }, h("div", { class: "spinner" }), h("div", {},
      h("b", {}, "试运行：上传一段 5 秒测试视频并填好标题和正文，最后一步不会点发布"), h("div", { class: "small muted" }, text))));
    const m = modal({ title: `试运行 · ${a.name}`, body: box, wide: true, footer: [h("button", { class: "btn", onclick: () => m.close() }, "关闭")], onClose: () => clearInterval(poll) });
    const poll = setInterval(async () => {
      const j = await get(`/api/social/jobs/${job.id}`).catch(() => null);
      if (!j) return;
      if (j.status === "running") { if (j.text) text.textContent = j.text; return; }
      clearInterval(poll);
      box.innerHTML = "";
      box.append(j.status === "done"
        ? h("div", { class: "notice", html: icon("check") }, h("div", {}, h("b", {}, "试运行成功"), "：上传、填写标题和正文都正常，真正发布时会多点一下「发布」按钮。",
          (j.warnings || []).length ? h("div", { class: "small muted" }, j.warnings.join("；")) : null))
        : h("div", { class: "notice err", html: icon("alert") }, h("div", {}, `试运行失败：${j.error}`)));
      if (j.screenshot) box.append(h("img", { src: rawUrl(j.screenshot), style: { width: "100%", borderRadius: "10px", marginTop: "10px", border: "1px solid var(--border)" } }));
      loadAccounts();
    }, 1500);
  }

  // ---- YouTube: the user's own Google OAuth client, then a normal Google sign-in per channel
  async function addYoutube(accountId) {
    const yt = store.settings?.social?.youtube || {};
    if (!yt.client_id || !yt.client_secret_set) return youtubeSetup(() => addYoutube(accountId));
    let r;
    try { r = await post("/api/social/youtube/login", { account_id: accountId || null }); } catch (e) { return toast(e.message, "err"); }
    window.open(r.auth_url, "_blank", "noopener");
    const before = new Map(accounts.map((a) => [a.id, a.updated_at]));
    const m = modal({
      title: accountId ? "重新登录 YouTube" : "添加 YouTube 账号",
      body: h("div", {},
        h("div", { class: "notice" }, h("div", { class: "spinner" }), h("div", {},
          h("b", {}, "请在新打开的 Google 页面里选择要连接的账号 / 频道，并允许授权"),
          h("div", { class: "small muted" }, "如果提示「Google 未验证此应用」，点「高级」→「转至…（不安全）」即可——这是你自己创建的应用。"),
          h("div", { class: "small", style: { marginTop: "6px" } }, h("a", { href: r.auth_url, target: "_blank", rel: "noopener" }, "没有弹出？点这里打开")))),
        h("div", { class: "hint", style: { marginTop: "10px" } }, "要添加多个频道（包括品牌账号），重复这一步并选择不同的频道即可。")),
      footer: [h("button", { class: "btn ghost", onclick: () => { m.close(); youtubeSetup(); } }, "修改 OAuth 设置"), h("button", { class: "btn", onclick: () => m.close() }, "关闭")],
      onClose: () => clearInterval(poll),
    });
    const started = Date.now();
    const poll = setInterval(async () => {
      const list = await get("/api/social/accounts").catch(() => null);
      if (!list) return;
      const changed = list.find((a) => a.platform === "youtube" && before.get(a.id) !== a.updated_at);
      if (changed) { clearInterval(poll); m.close(); toast(`YouTube 频道「${changed.name}」已连接`, "ok"); loadAccounts(); }
      else if (Date.now() - started > 600000) clearInterval(poll);
    }, 2000);
  }

  function youtubeSetup(next) {
    const yt = store.settings?.social?.youtube || {};
    const json = h("textarea", { class: "textarea mono", rows: 4, placeholder: '{"installed":{"client_id":"…apps.googleusercontent.com","client_secret":"…"}}' });
    const cid = h("input", { class: "input mono", value: yt.client_id || "", placeholder: "xxxx.apps.googleusercontent.com" });
    const sec = h("input", { class: "input mono", type: "password", placeholder: yt.client_secret_set ? "已保存（不修改请留空）" : "GOCSPX-…" });
    json.addEventListener("input", () => {
      try {
        const d = JSON.parse(json.value);
        const c = d.installed || d.web || d;
        if (c.client_id) cid.value = c.client_id;
        if (c.client_secret) sec.value = c.client_secret;
      } catch { /* still typing */ }
    });
    const link = (href, text) => h("a", { href, target: "_blank", rel: "noopener" }, text);
    const steps = h("ol", { class: "setup-steps" },
      h("li", {}, link(GCP.project, "新建一个 Google Cloud 项目"), "（名字随意，例如 AutoStudio）"),
      h("li", {}, "在项目里 ", link(GCP.api, "启用 YouTube Data API v3")),
      h("li", {}, "打开 ", link(GCP.consent, "Google Auth Platform"), "，按提示配置：应用名称填 AutoStudio，用户类型选「外部」，填上你的邮箱"),
      h("li", {}, "在 ", link(GCP.audience, "目标对象（Audience）"), " 里点「发布应用」，把状态改成「正式版 / In production」",
        h("div", { class: "small muted" }, "不改的话 Google 会在 7 天后让授权失效，需要反复重新登录。")),
      h("li", {}, "在 ", link(GCP.clients, "客户端（Clients）"), " 里创建 OAuth 客户端，应用类型选「桌面应用」，创建后点「下载 JSON」"),
      h("li", {}, "把下载的 JSON 文件内容粘贴到下面（会自动识别），保存后就能登录频道了"));
    const m = modal({
      title: "设置 YouTube 授权（只需要做一次）", wide: true,
      body: h("div", {},
        h("p", { class: "small muted", style: { marginTop: 0 } },
          "YouTube 发布走官方 API。开源软件没法内置统一的 Google 授权，所以需要你用自己的 Google 账号创建一个免费的 OAuth 客户端，大约 5 分钟。"),
        steps,
        h("div", { class: "field" }, h("label", {}, "粘贴 client_secret_….json 的内容"), json),
        h("div", { class: "row wrap", style: { gap: "10px" } },
          h("div", { class: "field grow" }, h("label", {}, "Client ID"), cid),
          h("div", { class: "field grow" }, h("label", {}, "Client Secret"), sec)),
        h("div", { class: "notice", html: icon("info") }, h("div", { class: "small" },
          "注意：Google 规定，未经过 YouTube API 审核的项目通过 API 上传的视频会被锁定为「私享」，需要在 YouTube 工作室里手动改成公开。",
          "想让视频自动公开，可以 ", link(GCP.audit, "提交 YouTube API 审核申请"), "（免费，需要几天到几周）。"))),
      footer: [h("button", { class: "btn", onclick: () => m.close() }, "取消"),
        h("button", { class: "btn primary", onclick: async () => {
          const payload = { client_id: cid.value.trim() };
          if (sec.value.trim()) payload.client_secret = sec.value.trim();
          if (!payload.client_id || (!payload.client_secret && !yt.client_secret_set)) return toast("请填写 Client ID 和 Client Secret", "err");
          if (!/\.apps\.googleusercontent\.com$/.test(payload.client_id)) return toast("Client ID 应该以 .apps.googleusercontent.com 结尾", "err");
          await put("/api/settings", { social: { youtube: payload } });
          await refreshSettings();
          m.close();
          toast("已保存", "ok");
          if (next) next();
        } }, next ? "保存并登录 YouTube" : "保存")],
    });
  }

  // ---- settings
  function renderSettings() {
    const s = store.settings;
    const perm = h("select", { class: "select", style: { maxWidth: "320px" } },
      [["ask", "每次先问我（推荐）"], ["allow", "直接发布，不用问"], ["deny", "禁止 Agent 发布"]].map(([v, l]) =>
        h("option", { value: v, selected: (s.permissions?.publish || "ask") === v }, l)));
    perm.onchange = async () => { await put("/api/settings", { permissions: { publish: perm.value } }); await refreshSettings(); toast("已保存", "ok"); };
    const show = h("input", { type: "checkbox", checked: s.social?.xhs_show_browser !== false });
    show.onchange = async () => { await put("/api/settings", { social: { xhs_show_browser: show.checked } }); await refreshSettings(); toast("已保存", "ok"); };
    settingsBox.innerHTML = "";
    settingsBox.append(h("div", { class: "section-title" }, "发布设置"), h("div", { class: "card" },
      h("div", { class: "field" }, h("label", {}, "在对话里，Agent 发布之前"), perm,
        h("div", { class: "hint" }, "定时任务不受这个设置影响：它们的内容会进入「待确认」，除非你在任务里打开了「自动发布」。")),
      h("label", { class: "row small", style: { gap: "10px", cursor: "pointer" } }, h("label", { class: "switch" }, show, h("span")),
        h("div", {}, h("b", {}, "发布到小红书时显示浏览器窗口"),
          h("div", { class: "muted" }, "更稳定，遇到安全验证时可以手动完成；关闭后在后台发布。"))),
      h("div", { class: "row", style: { marginTop: "12px" } },
        h("button", { class: "btn sm ghost", html: icon("key") + "YouTube OAuth 设置", onclick: () => youtubeSetup() }))));
  }

  await Promise.all([loadAccounts(), loadPosts()]);
  renderSettings();
  timer = setInterval(() => {
    if (document.visibilityState === "visible" && posts.some((p) => p.status === "publishing")) loadPosts();
  }, 2500);
}
