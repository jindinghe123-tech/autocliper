// Small, safe Markdown renderer (escapes all HTML first).
import { copyText } from "./dom.js";

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

function inline(s) {
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = esc(s);
  s = s.replace(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g, '<img alt="$1" src="$2" style="max-width:100%;border-radius:8px">');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(^|[\s(（])(https?:\/\/[^\s<)）]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
  return s;
}

function table(lines) {
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const head = cells(lines[0]);
  const rows = lines.slice(2).map(cells);
  return `<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows
    .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

export function markdown(src) {
  if (!src) return "";
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```(\S*)/);
    if (fence) {
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code data-lang="${esc(fence[1] || "")}">${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    if (/^\s*$/.test(line)) { i++; continue; }
    const hd = line.match(/^(#{1,6})\s+(.*)$/);
    if (hd) { const n = Math.min(4, hd[1].length); out.push(`<h${n}>${inline(hd[2])}</h${n}>`); i++; continue; }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const buf = [];
      while (i < lines.length && lines[i].includes("|")) buf.push(lines[i++]);
      out.push(table(buf));
      continue;
    }
    if (/^\s*>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${markdown(buf.join("\n"))}</blockquote>`);
      continue;
    }
    const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      const ordered = /\d/.test(li[2]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (m) { items.push({ indent: m[1].length, text: m[3] }); i++; continue; }
        if (/^\s{2,}\S/.test(lines[i]) && items.length) { items[items.length - 1].text += "\n" + lines[i].trim(); i++; continue; }
        break;
      }
      const base = items[0].indent;
      let html = `<${ordered ? "ol" : "ul"}>`;
      let depth = 0;
      for (const it of items) {
        const d = it.indent > base ? 1 : 0;
        if (d > depth) html += "<ul>";
        if (d < depth) html += "</ul>";
        depth = d;
        const task = it.text.match(/^\[([ xX])\]\s+(.*)/);
        const body = task ? `${task[1] === " " ? "☐" : "☑"} ${inline(task[2])}` : inline(it.text).replace(/\n/g, "<br>");
        html += `<li>${body}</li>`;
      }
      if (depth) html += "</ul>";
      out.push(html + `</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    const buf = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^\s*```/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i])
      && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) && !/^\s*>/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) { buf.push(lines[i++]); }
    out.push(`<p>${buf.map(inline).join("<br>")}</p>`);
  }
  return out.join("\n");
}

export function enhanceCode(root) {
  root.querySelectorAll("pre").forEach((pre) => {
    if (pre.querySelector(".copy")) return;
    const b = document.createElement("button");
    b.className = "btn sm copy";
    b.textContent = "复制";
    b.onclick = () => {
      copyText(pre.querySelector("code")?.innerText || "");
      b.textContent = "已复制";
      setTimeout(() => (b.textContent = "复制"), 1200);
    };
    pre.append(b);
  });
}
