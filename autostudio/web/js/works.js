// 作品: the videos AutoStudio made — delivered cuts (成片 + 封面 + 发布文案) first, then every other video.
import { downloadUrl, get, rawUrl, thumbUrl } from "./api.js";
import { copyText, fmtSize, fmtTime, h, icon, modal, toast } from "./dom.js";
import { reveal } from "./preview.js";
import { isRemote, navigate, store } from "./store.js";

let tab = "delivered";

export async function showWorks(main) {
  main.innerHTML = "";
  const tabs = h("div", { class: "tabs" });
  const body = h("div");
  main.append(
    h("div", { class: "page-head" },
      h("button", { class: "btn ghost icon-only menu-btn", html: icon("menu"), onclick: () => document.getElementById("sidebar").classList.toggle("open") }),
      h("h1", {}, "作品"), h("div", { class: "grow" }),
      h("button", { class: "btn sm", html: icon("refresh") + "刷新", onclick: () => load() })),
    h("div", { class: "page-body" }, h("div", { style: { maxWidth: "1200px", margin: "0 auto" } }, tabs, body)));

  let data = null;
  async function load() {
    body.innerHTML = '<div class="empty"><div class="spinner" style="margin:0 auto"></div></div>';
    try { data = await get("/api/works"); } catch (e) { body.innerHTML = ""; toast(e.message, "err"); return; }
    if (tab === "delivered" && !data.delivered.length && data.videos.length) tab = "videos";
    render();
  }

  function render() {
    tabs.innerHTML = "";
    for (const [k, label] of [["delivered", `成片 · ${data.delivered.length}`], ["videos", `全部视频 · ${data.total}`]])
      tabs.append(h("button", { class: k === tab ? "active" : "", onclick: () => { tab = k; render(); } }, label));
    body.innerHTML = "";
    const list = tab === "delivered" ? data.delivered : data.videos;
    if (!list.length) {
      body.append(h("div", { class: "empty" }, tab === "delivered"
        ? "还没有交付的成片。在对话里让 AutoStudio 做一条视频，交付后会出现在这里。"
        : "工作区里还没有视频。"));
      return;
    }
    body.append(h("div", { class: "work-grid" }, list.map(card)));
  }

  function card(it) {
    const thumb = h("div", { class: "work-thumb" },
      h("img", { src: thumbUrl(it.cover || it.path, 480), loading: "lazy", alt: "",
        onerror: (e) => { e.target.replaceWith(h("span", { class: "work-noimg", html: icon("film") })); } }),
      h("span", { class: "work-play", html: icon("play") }));
    return h("div", { class: "work", onclick: () => openWork(it) }, thumb,
      h("div", { class: "work-meta" },
        h("div", { class: "work-title", title: it.title || it.name }, it.title || it.name),
        h("div", { class: "work-sub" }, [it.folder, fmtTime(it.mtime), fmtSize(it.size)].filter(Boolean).join(" · "))));
  }

  load();
}

export function openWork(it) {
  const video = h("video", { src: rawUrl(it.path), controls: true, autoplay: true, playsinline: true, preload: "metadata",
    poster: it.cover ? thumbUrl(it.cover, 720) : null });
  const desc = it.description
    ? h("div", { class: "work-desc" }, h("div", { class: "row", style: { marginBottom: "6px" } },
        h("b", { class: "grow" }, "发布文案"),
        h("button", { class: "btn sm", html: icon("copy") + "复制", onclick: () =>
          copyText(`${it.title}\n\n${it.description}`).then(() => toast("已复制", "ok"), () => toast("复制失败", "err")) })),
      h("div", { class: "work-desc-text" }, it.description))
    : null;
  const m = modal({
    title: it.title || it.name, wide: true,
    body: h("div", { class: "work-view" }, h("div", { class: "work-player" }, video), desc),
    onClose: () => video.pause(),
    footer: [
      isRemote() ? null : h("button", { class: "btn", html: icon("folder") + "在文件夹中显示", onclick: () => reveal(it.path) }),
      h("button", { class: "btn", html: icon("chat") + "继续修改", onclick: () => {
        m.close();
        store.composerDraft = { text: "", attachments: [it.path] };
        navigate("#/chat");
      } }),
      h("a", { class: "btn primary", href: downloadUrl(it.path, it.title || ""), download: it.title ? `${it.title}.mp4` : it.name,
        html: icon("download") + (isRemote() ? "保存到手机" : "下载") }),
    ].filter(Boolean),
  });
}
