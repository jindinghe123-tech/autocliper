// Workspace file browser.
import { get, rawUrl, upload } from "./api.js";
import { KIND_ICON, fmtSize, fmtTime, h, icon, modal, toast } from "./dom.js";
import { previewFile, reveal, subtitleEditorHash } from "./preview.js";
import { navigate, store } from "./store.js";

let currentPath = "";

export async function showFiles(main, params) {
  main.innerHTML = "";
  if (params.has("path")) currentPath = params.get("path");
  const grid = h("div", { class: "file-grid" });
  const crumbs = h("div", { class: "crumbs" });
  const input = h("input", { type: "file", multiple: true, style: { display: "none" } });
  input.onchange = async () => {
    if (!input.files.length) return;
    try { await upload(input.files); toast("上传完成（已保存到 uploads/）", "ok"); load(currentPath); } catch (e) { toast(e.message, "err"); }
    input.value = "";
  };
  main.append(
    h("div", { class: "page-head" },
      h("button", { class: "btn ghost icon-only menu-btn", html: icon("menu"), onclick: () => document.getElementById("sidebar").classList.toggle("open") }),
      h("h1", {}, "文件"), h("div", { class: "grow" }),
      h("button", { class: "btn sm", html: icon("refresh"), title: "刷新", onclick: () => load(currentPath) }),
      h("button", { class: "btn sm", html: icon("folder") + "在资源管理器中打开", onclick: () => reveal(currentPath || store.meta?.workspace) }),
      h("button", { class: "btn sm primary", html: icon("upload") + "上传", onclick: () => input.click() }), input),
    h("div", { class: "page-body" }, h("div", { style: { maxWidth: "1200px", margin: "0 auto" } },
      h("div", { class: "row", style: { marginBottom: "16px" } }, crumbs), grid)));

  async function load(path) {
    let res;
    try { res = await get(`/api/files?path=${encodeURIComponent(path || "")}`); } catch (e) { toast(e.message, "err"); return; }
    currentPath = res.path;
    crumbs.innerHTML = "";
    crumbs.append(h("a", { onclick: () => load("") , html: icon("folder") + " 工作区" }));
    if (res.rel) {
      const parts = res.rel.split("/");
      let acc = "";
      for (const p of parts) {
        acc = acc ? `${acc}/${p}` : p;
        const target = acc;
        crumbs.append(h("span", { class: "muted" }, "/"), h("a", { onclick: () => load(target) }, p));
      }
    }
    grid.innerHTML = "";
    if (res.parent) grid.append(tile({ name: "..", is_dir: true, kind: "dir", path: res.parent }));
    for (const it of res.items) grid.append(tile(it));
    if (!res.items.length) grid.append(h("div", { class: "empty", style: { gridColumn: "1/-1" } }, "空文件夹。把视频拖到对话框里上传，或点击右上角「上传」。"));
  }

  function tile(it) {
    const thumb = h("div", { class: "thumb" });
    if (it.kind === "image") thumb.append(h("img", { src: rawUrl(it.path), loading: "lazy" }));
    else if (it.kind === "video") thumb.append(h("video", { src: rawUrl(it.path) + "#t=0.5", preload: "metadata", muted: true }));
    else thumb.innerHTML = icon(KIND_ICON[it.kind] || "file");
    const acts = h("div", { class: "acts" });
    if (!it.is_dir) {
      acts.append(h("button", { class: "btn sm icon-only", title: "在对话中使用", html: icon("chat"), onclick: (e) => {
        e.stopPropagation();
        store.composerDraft = { text: "", attachments: [it.path] };
        navigate("#/chat");
      } }));
    }
    return h("div", { class: "file", onclick: () => open(it) }, thumb, acts,
      h("div", { class: "meta" }, h("div", { class: "n", title: it.name }, it.name),
        h("div", { class: "s" }, it.is_dir ? "文件夹" : `${fmtSize(it.size)} · ${fmtTime(it.mtime)}`)));
  }

  async function open(it) {
    if (it.is_dir) return load(it.path);
    if (it.kind === "subtitle") return navigate(subtitleEditorHash(it.path));
    let body;
    if (it.kind === "text") {
      const res = await fetch(rawUrl(it.path));
      body = h("pre", { class: "mono", style: { whiteSpace: "pre-wrap", maxHeight: "65vh", overflow: "auto", margin: 0 } }, (await res.text()).slice(0, 200000));
    } else {
      body = previewFile(it.path);
    }
    modal({ title: it.name, body, wide: true,
      footer: [h("button", { class: "btn", html: icon("chat") + "在对话中使用", onclick: () => {
        document.querySelector(".modal-bg")?.remove();
        store.composerDraft = { text: "", attachments: [it.path] };
        navigate("#/chat");
      } })] });
  }

  load(currentPath);
}
