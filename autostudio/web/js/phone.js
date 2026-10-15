// 手机连接: pair phones with this PC. On the PC it shows the QR code / pairing code and the paired
// devices; on a paired phone it shows which PC it is connected to and lets it disconnect.
import { api, get, patch, post, put } from "./api.js";
import { confirmDialog, copyText, fmtTime, h, icon, toast } from "./dom.js";
import { inPhoneApp, isRemote, store } from "./store.js";

let pollTimer = 0;
let pickedIp = "";

export function stopPhoneView() {
  clearInterval(pollTimer);
  pollTimer = 0;
}

export async function showPhone(main) {
  stopPhoneView();
  main.innerHTML = "";
  const body = h("div", { class: "page-narrow" });
  main.append(
    h("div", { class: "page-head" },
      h("button", { class: "btn ghost icon-only menu-btn", html: icon("menu"), onclick: () => document.getElementById("sidebar").classList.toggle("open") }),
      h("h1", {}, "手机连接")),
    h("div", { class: "page-body" }, body));
  if (isRemote()) return renderOnPhone(body);
  return renderOnPc(body);
}

// ---------------------------------------------------------------------------
// on a paired phone

export async function disconnectThisPhone() {
  if (!(await confirmDialog("断开这台手机和电脑的连接？之后要在电脑上重新配对才能再连。", { danger: true, okText: "断开" }))) return;
  try { await post("/api/remote/logout"); } catch { /* already gone */ }
  location.href = inPhoneApp() ? "autostudio://disconnected" : "/pair";
}

function renderOnPhone(body) {
  const r = store.meta.remote;
  body.append(
    h("div", { class: "card phone-hero" },
      h("div", { class: "phone-hero-ico", html: icon("monitor") }),
      h("div", { class: "grow" },
        h("h3", {}, `已连接到「${r.pc_name}」`),
        h("div", { class: "small muted" }, `这台设备：${r.device?.name || "手机"}。你在这里做的一切都在电脑上运行，视频也保存在电脑上。`))),
    h("div", { class: "row wrap", style: { marginTop: "14px" } },
      inPhoneApp() ? h("button", { class: "btn", html: icon("refresh") + "切换电脑", onclick: () => { location.href = "autostudio://home"; } }) : null,
      h("button", { class: "btn danger", html: icon("logout") + "断开连接", onclick: disconnectThisPhone })),
    h("div", { class: "notice", style: { marginTop: "18px" } }, h("span", { html: icon("info") }),
      h("div", {}, "配对新设备、修改端口、管理已连接的设备，只能在电脑上的「手机连接」页面操作。")));
}

// ---------------------------------------------------------------------------
// on the PC

async function renderOnPc(body) {
  let st;
  try { st = await get("/api/remote"); } catch (e) { toast(e.message, "err"); return; }
  const known = new Set(st.devices.map((d) => d.id));

  const draw = () => {
    body.innerHTML = "";
    body.append(heroCard(), st.enabled ? (st.running ? pairCard() : errorCard()) : null, devicesCard(), optionsCard());
  };

  const setEnabled = async (on, sw) => {
    sw.disabled = true;
    try { st = await put("/api/remote", { enabled: on }); } catch (e) { toast(e.message, "err"); }
    draw();
    if (st.running) checkFirewall(1);
  };

  function heroCard() {
    const sw = h("input", { type: "checkbox", checked: st.enabled });
    sw.onchange = () => setEnabled(sw.checked, sw);
    return h("div", { class: "card phone-hero" },
      h("div", { class: "phone-hero-ico", html: icon("phone") }),
      h("div", { class: "grow" },
        h("h3", {}, "用手机远程使用这台电脑"),
        h("div", { class: "small muted" }, "手机上看到的就是这个界面：发任务、看进度、看做好的视频。所有任务都在这台电脑上跑。手机必须先在这里配对才能连接。")),
      h("label", { class: "switch", title: st.enabled ? "关闭手机连接" : "开启手机连接" }, sw, h("span")));
  }

  function errorCard() {
    return h("div", { class: "notice err" }, h("span", { html: icon("alert") }),
      h("div", {}, st.error || "手机连接服务没有运行。", " ",
        h("a", { href: "javascript:void 0", onclick: () => setEnabled(true, { }) }, "重试")));
  }

  function pairCard() {
    const addrs = st.addresses;
    if (!addrs.length) {
      return h("div", { class: "notice err" }, h("span", { html: icon("wifi") }),
        h("div", {}, "这台电脑现在没有连上任何网络。连上 Wi-Fi 或网线后，手机才能找到它。"));
    }
    if (!addrs.some((a) => a.ip === pickedIp)) pickedIp = addrs[0].ip;
    const addr = addrs.find((a) => a.ip === pickedIp);
    const code = st.pairing?.code || "------";
    const qr = h("img", { class: "pair-qr", alt: "配对二维码", src: `/api/remote/qr.svg?ip=${encodeURIComponent(pickedIp)}&c=${code}`,
      onerror: (e) => { e.target.replaceWith(h("div", { class: "pair-qr empty small" }, "二维码组件未安装，请用下面的地址和配对码手动连接")); } });
    const ipSel = addrs.length > 1
      ? h("select", { class: "select", onchange: (e) => { pickedIp = e.target.value; draw(); } },
          addrs.map((a) => h("option", { value: a.ip, selected: a.ip === pickedIp }, `${a.ip}（${a.label}）`)))
      : null;
    const mins = Math.max(1, Math.ceil((st.pairing?.expires_in || 0) / 60));
    return h("div", { class: "card pair-card" },
      h("div", { class: "pair-left" }, qr,
        h("div", { class: "pair-code-label" }, "配对码"),
        h("div", { class: "pair-code mono" }, `${code.slice(0, 3)} ${code.slice(3)}`),
        h("div", { class: "small muted" }, `${mins} 分钟内有效 · 用过一次就换新的 `,
          h("a", { href: "javascript:void 0", onclick: async () => { st = await post("/api/remote/pairing/refresh"); draw(); } }, "换一个"))),
      h("div", { class: "pair-right" },
        h("h3", {}, "怎么连"),
        h("ol", { class: "setup-steps" },
          h("li", {}, "手机和这台电脑连在", h("b", {}, "同一个 Wi-Fi"), "（或同一个路由器）。"),
          h("li", {}, h("b", {}, "安卓："), "打开 AutoStudio App，点「扫码连接」扫左边的二维码。还没装 App？用手机相机或浏览器扫码，打开的页面里就有下载按钮。"),
          h("li", {}, h("b", {}, "iPhone："), "用相机扫码，在 Safari 里输入配对码；连上后点「分享 → 添加到主屏幕」，以后就像 App 一样打开。"),
          h("li", {}, "扫不了码？在 App 里点「搜索电脑」，或在手机浏览器打开下面的地址，再输入配对码。")),
        h("div", { class: "field" }, h("label", {}, "手机访问地址"),
          ipSel,
          h("div", { class: "row" },
            h("code", { class: "pair-url grow" }, addr.url),
            h("button", { class: "btn sm", html: icon("copy") + "复制", onclick: () => copyText(addr.url).then(() => toast("已复制", "ok")) }))),
        firewallBox));
  }

  // Windows firewall check: a dismissed "allow access" prompt silently keeps every phone out
  const firewallBox = h("div", { class: "small muted", style: { marginTop: "4px" } }, "正在检查 Windows 防火墙…");
  const showFirewall = (fw) => {
    firewallBox.innerHTML = "";
    if (!fw || !fw.checked) {
      firewallBox.append("如果手机连不上，检查电脑的防火墙是否允许 AutoStudio（Python）接受连接。");
    } else if (fw.ok) {
      firewallBox.append(h("span", { class: "tag ok" }, "Windows 防火墙已放行"));
    } else {
      const btn = h("button", { class: "btn sm primary", html: icon("check") + "一键放行" });
      btn.onclick = async () => {
        btn.disabled = true;
        btn.innerHTML = '<div class="spinner"></div>请在弹出的窗口里点「是」';
        try { showFirewall(await post("/api/remote/firewall/fix")); toast("已放行，手机现在可以连了", "ok"); }
        catch (e) { toast(e.message, "err"); btn.disabled = false; btn.innerHTML = icon("check") + "一键放行"; }
      };
      firewallBox.append(h("div", { class: "notice err", style: { margin: 0 } }, h("span", { html: icon("alert") }),
        h("div", {}, `Windows 防火墙会拦住手机（当前是${fw.blocked_on.join("、")}）。点一下放行：只对局域网开放手机连接端口，需要在弹出的管理员窗口里点「是」。`,
          h("div", { style: { marginTop: "8px" } }, btn))));
    }
  };
  const checkFirewall = (refresh = 0) => get(`/api/remote/firewall?refresh=${refresh}`).then(showFirewall, () => showFirewall(null));

  function devicesCard() {
    const card = h("div", { class: "card" }, h("h3", {}, `已配对的设备（${st.devices.length}）`));
    if (!st.devices.length) {
      card.append(h("div", { class: "small muted", style: { marginTop: "6px" } }, "还没有手机连过这台电脑。"));
      return card;
    }
    const list = h("div", { class: "dev-list" });
    for (const d of st.devices) {
      const online = d.last_seen && Date.now() / 1000 - d.last_seen < 90;
      list.append(h("div", { class: "dev" },
        h("div", { class: "dev-ico", html: icon("phone") }),
        h("div", { class: "grow" },
          h("div", { class: "dev-name", title: "点击改名", onclick: async () => {
            const name = prompt("给这台设备起个名字", d.name);
            if (!name || name.trim() === d.name) return;
            try { st = await patch(`/api/remote/devices/${d.id}`, { name: name.trim() }); draw(); } catch (e) { toast(e.message, "err"); }
          } }, d.name, " ", h("span", { class: `tag ${online ? "ok" : ""}` }, online ? "在线" : d.platform === "android" ? "安卓 App" : "浏览器")),
          h("div", { class: "small muted" }, `最近使用 ${d.last_seen ? fmtTime(d.last_seen) : "—"}${d.last_ip ? ` · ${d.last_ip}` : ""} · ${fmtTime(d.created)} 配对`)),
        h("button", { class: "btn sm danger", html: icon("logout") + "断开", onclick: async () => {
          if (!(await confirmDialog(`断开「${d.name}」？这台设备会立刻失去访问权限，要重新配对才能再连。`, { danger: true, okText: "断开" }))) return;
          try { st = await api(`/api/remote/devices/${d.id}`, { method: "DELETE" }); known.delete(d.id); draw(); } catch (e) { toast(e.message, "err"); }
        } })));
    }
    card.append(list);
    return card;
  }

  function optionsCard() {
    const name = h("input", { class: "input", value: st.pc_name, maxlength: 40 });
    const port = h("input", { class: "input", value: st.port, type: "number", min: 1024, max: 65535, style: { maxWidth: "140px" } });
    return h("div", { class: "card" },
      h("h3", {}, "设置"),
      h("div", { class: "row wrap", style: { alignItems: "flex-end", marginTop: "10px" } },
        h("div", { class: "field grow", style: { marginBottom: 0, minWidth: "200px" } }, h("label", {}, "电脑名称（手机上显示）"), name),
        h("div", { class: "field", style: { marginBottom: 0 } }, h("label", {}, "端口"), port),
        h("button", { class: "btn", onclick: async () => {
          try {
            st = await put("/api/remote", { pc_name: name.value, port: Number(port.value) });
            toast("已保存", "ok");
            draw();
          } catch (e) { toast(e.message, "err"); }
        } }, "保存")),
      h("div", { class: "hint small muted", style: { marginTop: "10px" } },
        "不在家也想用？在电脑和手机上都装好 Tailscale 并登录同一个账号，上面的地址里会多出一个 100.x 开头的地址，在外面用它就能连。"));
  }

  draw();
  if (st.running) checkFirewall();
  pollTimer = setInterval(async () => {
    if (document.visibilityState !== "visible" || document.activeElement?.tagName === "INPUT") return;
    try {
      const next = await get("/api/remote");
      for (const d of next.devices) if (!known.has(d.id)) { known.add(d.id); toast(`「${d.name}」已连接到这台电脑`, "ok"); }
      const changed = JSON.stringify([next.pairing?.code, next.running, next.enabled, next.devices, next.addresses])
        !== JSON.stringify([st.pairing?.code, st.running, st.enabled, st.devices, st.addresses]);
      st = next;
      if (changed && !document.querySelector(".modal-bg")) draw();
    } catch { /* server restarting */ }
  }, 2500);
}
