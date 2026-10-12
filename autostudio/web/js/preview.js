// Inline preview for files produced by tools.
import { post, rawUrl } from "./api.js";
import { basename, h, icon, kindOf, toast } from "./dom.js";
import { navigate } from "./store.js";

export function reveal(path) {
  post("/api/files/reveal", { path }).catch((e) => toast(e.message, "err"));
}

export function subtitleEditorHash(sub, media) {
  return `#/subtitles?path=${encodeURIComponent(sub)}${media ? `&media=${encodeURIComponent(media)}` : ""}`;
}

export function previewFile(path, { editorMedia, compact = false } = {}) {
  const kind = kindOf(path);
  const v = Date.now();
  const url = rawUrl(path) + `&v=${v}`;
  let media = null;
  if (kind === "video") media = h("video", { src: url, controls: true, preload: "metadata", playsinline: true });
  else if (kind === "audio") media = h("audio", { src: url, controls: true, preload: "metadata" });
  else if (kind === "image") media = h("img", { src: url, loading: "lazy", onclick: () => window.open(url, "_blank"), style: { cursor: "zoom-in" } });
  const actions = [];
  if (kind === "subtitle") {
    actions.push(h("button", { class: "btn sm primary", html: icon("captions") + "字幕编辑器",
      onclick: () => navigate(subtitleEditorHash(path, editorMedia)) }));
  }
  actions.push(h("a", { class: "btn sm ghost icon-only", title: "下载", href: rawUrl(path), download: basename(path), html: icon("download") }));
  actions.push(h("button", { class: "btn sm ghost icon-only", title: "在文件夹中显示", html: icon("folder"), onclick: () => reveal(path) }));
  const ico = { video: "film", audio: "music", image: "image", subtitle: "captions", text: "text" }[kind] || "file";
  return h("div", { class: `preview ${media ? "" : "compact"} ${compact ? "compact" : ""}` },
    compact ? null : media,
    h("div", { class: "preview-bar" }, h("span", { html: icon(ico) }), h("span", { class: "name", title: path }, basename(path)), actions));
}
