// Scheduled tasks page.
import { del, get, post, put } from "./api.js";
import { confirmDialog, fmtTime, h, icon, iconEl, modal, toast } from "./dom.js";
import { modelOptions, navigate, refreshSessions } from "./store.js";

const TEMPLATES = [
  { icon: "globe", name: "每日科技简报", cron: "0 9 * * *",
    prompt: "抓取 https://news.ycombinator.com 和 https://www.36kr.com/newsflashes 的最新内容，挑出最重要的 10 条科技/AI 新闻，每条用一两句中文概括并附上链接，整理成 Markdown 简报保存到 reports/简报-今天日期.md，最后输出简报全文。" },
  { icon: "captions", name: "自动给新视频加字幕", cron: "0 * * * *",
    prompt: "检查工作区 inbox/ 文件夹中的视频文件（没有该文件夹就创建并结束）。对每个视频：语音识别生成中文字幕，通读修正明显错别字，用默认样式烧录字幕，输出到 outputs/字幕版/，然后把原视频移动到 inbox/done/。最后汇报处理了哪些文件。" },
  { icon: "film", name: "每周素材整理", cron: "0 20 * * 0",
    prompt: "整理工作区 downloads/ 和 uploads/ 文件夹：按类型（视频/音频/图片/字幕/其它）统计数量和总大小，找出超过 500MB 的大文件和 30 天前的旧文件，生成一份整理建议报告保存到 reports/，不要删除任何文件。" },
  { icon: "upload", name: "素材自动剪辑并发布", cron: "0 18 * * *", watch_dir: "素材",
    prompt: "把素材文件夹里新放进来的素材做成一条适合小红书的短视频，写好标题、正文和话题，发布到我的小红书账号。" },
  { icon: "sparkles", name: "每日短视频选题", cron: "30 8 * * 1-5",
    prompt: "抓取 https://www.zhihu.com/billboard 和 https://top.baidu.com/board?tab=realtime 的热榜，结合“科技/效率工具/AI”方向，给出 5 个适合做 60 秒短视频的选题，每个包含：标题、3 秒开头钩子、内容大纲、推荐 BGM 风格。保存到 reports/选题-日期.md 并输出。" },
];

const PRESETS = [
  ["daily", "每天"], ["weekday", "工作日"], ["weekly", "每周"], ["hourly", "每小时"], ["custom", "自定义 cron"],
];
const WEEKDAYS = [["1", "周一"], ["2", "周二"], ["3", "周三"], ["4", "周四"], ["5", "周五"], ["6", "周六"], ["0", "周日"]];

function cronToText(cron) {
  const m = String(cron).trim().split(/\s+/);
  if (m.length !== 5) return cron;
  const [mi, hr, dom, mon, dow] = m;
  const t = /^\d+$/.test(hr) && /^\d+$/.test(mi) ? `${hr.padStart(2, "0")}:${mi.padStart(2, "0")}` : null;
  if (t && dom === "*" && mon === "*" && dow === "*") return `每天 ${t}`;
  if (t && dom === "*" && mon === "*" && dow === "1-5") return `工作日 ${t}`;
  if (t && dom === "*" && mon === "*" && /^[0-6]$/.test(dow)) return `每${WEEKDAYS.find((w) => w[0] === dow)?.[1] || "周" + dow} ${t}`;
  if (/^\d+$/.test(mi) && hr === "*" && dom === "*" && dow === "*") return `每小时第 ${mi} 分`;
  if (/^\*\/\d+$/.test(hr) && /^\d+$/.test(mi)) return `每 ${hr.slice(2)} 小时`;
  return cron;
}

function parseCron(cron) {
  const m = String(cron || "0 9 * * *").trim().split(/\s+/);
  const [mi, hr, dom, mon, dow] = m;
  const time = /^\d+$/.test(hr) && /^\d+$/.test(mi) ? `${hr.padStart(2, "0")}:${mi.padStart(2, "0")}` : "09:00";
  if (m.length === 5 && dom === "*" && mon === "*") {
    if (/^\d+$/.test(hr) && dow === "*") return { preset: "daily", time };
    if (/^\d+$/.test(hr) && dow === "1-5") return { preset: "weekday", time };
    if (/^\d+$/.test(hr) && /^[0-6]$/.test(dow)) return { preset: "weekly", time, dow };
    if (hr === "*" && dow === "*" && /^\d+$/.test(mi)) return { preset: "hourly", time: `00:${mi.padStart(2, "0")}` };
  }
  return { preset: "custom", time, cron };
}

function taskForm(initial, onSaved) {
  const task = initial && !initial.isTemplate ? initial : null; // templates prefill a *new* task
  const t = initial || { name: "", prompt: "", cron: "0 9 * * *", enabled: true, webhook: "", allow_dangerous: false, auto_publish: false, watch_dir: "" };
  const pc = parseCron(t.cron);
  const name = h("input", { class: "input", value: t.name, placeholder: "例如：每日科技简报" });
  const prompt = h("textarea", { class: "textarea", rows: 7, placeholder: "写清楚每次要做什么、素材在哪、结果保存到哪里…" });
  prompt.value = t.prompt;
  const preset = h("select", { class: "select" }, PRESETS.map(([v, l]) => h("option", { value: v, selected: v === pc.preset }, l)));
  const time = h("input", { class: "input", type: "time", value: pc.time, style: { width: "130px" } });
  const dow = h("select", { class: "select", style: { width: "110px" } }, WEEKDAYS.map(([v, l]) => h("option", { value: v, selected: v === (pc.dow || "1") }, l)));
  const cron = h("input", { class: "input mono", value: t.cron, placeholder: "分 时 日 月 周", style: { width: "180px" } });
  const preview = h("div", { class: "hint" });
  const webhook = h("input", { class: "input", value: t.webhook || "", placeholder: "https://open.feishu.cn/open-apis/bot/v2/hook/…（可选）" });
  const watch = h("input", { class: "input mono", value: t.watch_dir || "", placeholder: "例如 D:\\素材，或只写「素材」= 工作区里的素材文件夹（可选）" });
  const dangerous = h("input", { type: "checkbox", checked: !!t.allow_dangerous });
  const autoPublish = h("input", { type: "checkbox", checked: !!t.auto_publish });
  const modelSel = h("select", { class: "select" }, h("option", { value: "" }, "使用默认模型"),
    modelOptions().filter((o) => o.ready).map((o) => h("option", { value: `${o.pid}|${o.model}`, selected: t.provider === o.pid && t.model === o.model }, `${o.name} · ${o.model}`)));

  const compute = () => {
    const [hh, mm] = (time.value || "09:00").split(":").map((x) => String(parseInt(x, 10)));
    const p = preset.value;
    if (p === "daily") cron.value = `${mm} ${hh} * * *`;
    if (p === "weekday") cron.value = `${mm} ${hh} * * 1-5`;
    if (p === "weekly") cron.value = `${mm} ${hh} * * ${dow.value}`;
    if (p === "hourly") cron.value = `${mm} * * * *`;
    time.style.display = p === "custom" ? "none" : "";
    dow.style.display = p === "weekly" ? "" : "none";
    cron.readOnly = p !== "custom";
    refreshPreview();
  };
  let timer = 0;
  const refreshPreview = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const r = await post("/api/cron/preview", { cron: cron.value });
      preview.textContent = r.ok ? `接下来运行：${r.next.join("  ·  ")}` : r.error;
      preview.style.color = r.ok ? "" : "var(--err)";
    }, 250);
  };
  [preset, time, dow].forEach((el) => el.addEventListener("change", compute));
  cron.addEventListener("input", refreshPreview);
  compute();

  const body = h("div", {},
    h("div", { class: "field" }, h("label", {}, "任务名称"), name),
    h("div", { class: "field" }, h("label", {}, "执行指令"), prompt,
      h("div", { class: "hint" }, "到点后 Agent 会以这段话为指令自动执行（无人值守，不会提问），可以使用所有工具。")),
    h("div", { class: "field" }, h("label", {}, "执行时间"), h("div", { class: "row wrap" }, preset, dow, time, cron), preview),
    h("div", { class: "field" }, h("label", {}, "素材文件夹"), watch,
      h("div", { class: "hint" }, "填了以后，每次只把上次之后新放进去的文件交给 Agent；没有新文件就不运行，也不会打扰你。第一次运行会处理文件夹里已有的文件。")),
    h("div", { class: "field" }, h("label", {}, "模型"), modelSel),
    h("div", { class: "field" }, h("label", {}, "结果推送 Webhook"), webhook,
      h("div", { class: "hint" }, "自动识别：飞书 / 钉钉 / 企业微信 / Telegram / Discord / Slack / Server酱 / Bark / ntfy，其它地址按通用 JSON 推送。")),
    h("label", { class: "row small", style: { gap: "8px", cursor: "pointer", marginBottom: "8px" } }, autoPublish,
      h("span", {}, "发布到小红书 / YouTube 时直接发出", h("span", { class: "muted" }, "（不勾选：先放进「待确认」，手机收到通知，你点一下批准才发）"))),
    h("label", { class: "row small", style: { gap: "8px", cursor: "pointer" } }, dangerous, "允许执行命令行等高风险操作（无人值守时不会询问你）"));

  const m = modal({
    title: task ? "编辑定时任务" : "新建定时任务", body,
    footer: [h("button", { class: "btn", onclick: () => m.close() }, "取消"),
      h("button", { class: "btn primary", onclick: async () => {
        const [provider, model] = modelSel.value ? modelSel.value.split("|") : [null, null];
        const payload = { name: name.value.trim(), prompt: prompt.value.trim(), cron: cron.value.trim(), webhook: webhook.value.trim() || null,
          allow_dangerous: dangerous.checked, auto_publish: autoPublish.checked, provider, model, enabled: t.enabled ?? true,
          watch_dir: watch.value.trim() || null };
        if (!payload.name || !payload.prompt) return toast("请填写名称和指令", "err");
        try {
          if (task) await put(`/api/tasks/${task.id}`, payload); else await post("/api/tasks", payload);
          m.close();
          toast("已保存", "ok");
          onSaved();
        } catch (e) { toast(e.message, "err"); }
      } }, "保存")],
  });
}

export async function showTasks(main) {
  main.innerHTML = "";
  const body = h("div", { class: "page-narrow" });
  main.append(
    h("div", { class: "page-head" },
      h("button", { class: "btn ghost icon-only menu-btn", html: icon("menu"), onclick: () => document.getElementById("sidebar").classList.toggle("open") }),
      h("h1", {}, "定时任务"), h("div", { class: "grow" }),
      h("button", { class: "btn primary sm", html: icon("plus") + "新建任务", onclick: () => taskForm(null, render) })),
    h("div", { class: "page-body" }, body));

  async function render() {
    let tasks;
    try { tasks = await get("/api/tasks"); } catch (e) { toast(e.message, "err"); return; }
    body.innerHTML = "";
    body.append(h("p", { class: "muted", style: { marginTop: 0 } },
      "让 Agent 按计划自动干活：定时抓取资讯、批量处理新素材、生成报告并推送到你的群聊。AutoStudio 需要保持运行，任务才会按时执行（错过 1 小时内会自动补跑）。"));
    const templates = () => h("div", { class: "suggest templates" }, TEMPLATES.map((tp) =>
      h("button", { class: "s", onclick: () => taskForm({ ...tp, enabled: true, isTemplate: true }, render) },
        h("div", { class: "ico", html: icon(tp.icon) }),
        h("div", {}, h("b", {}, tp.name), h("span", {}, `${cronToText(tp.cron)} · ${tp.prompt.slice(0, 36)}…`)))));
    if (!tasks.length) {
      body.append(h("div", { class: "section-title" }, "从模板开始"), templates(),
        h("div", { class: "small muted" }, "也可以直接在对话里说：「每天早上 9 点帮我……」，Agent 会自动创建任务。"));
      return;
    }
    for (const t of tasks) body.append(taskCard(t));
    body.append(h("div", { class: "section-title" }, "更多模板"), templates());
  }

  function taskCard(t) {
    const runsBox = h("div", { class: "runs", style: { display: "none" } });
    const toggle = h("input", { type: "checkbox", checked: t.enabled });
    toggle.onchange = async () => { await put(`/api/tasks/${t.id}`, { enabled: toggle.checked }); render(); };
    const status = t.last_status === "ok" ? h("span", { class: "tag ok" }, "上次成功")
      : t.last_status === "error" ? h("span", { class: "tag err" }, "上次失败")
        : t.last_status === "running" ? h("span", { class: "tag accent" }, "运行中")
          : t.last_status === "skipped" ? h("span", { class: "tag" }, "上次没有新素材") : null;
    const card = h("div", { class: "card task-card" },
      h("div", { class: "tico", html: icon("clock") }),
      h("div", { class: "grow" },
        h("h3", {}, t.name, status),
        h("div", { class: "row wrap small muted", style: { gap: "12px" } },
          h("span", { html: icon("clock") + " " + cronToText(t.cron) }),
          t.enabled && t.next_run ? h("span", {}, `下次：${t.next_run}`) : h("span", {}, "已停用"),
          t.last_run_at ? h("span", {}, `上次：${fmtTime(t.last_run_at)}`) : null,
          t.webhook ? h("span", { class: "tag" }, "推送") : null,
          t.auto_publish ? h("span", { class: "tag warn" }, "自动发布") : null,
          t.watch_dir ? h("span", { class: "tag", title: t.watch_dir }, iconEl("folder"), " " + t.watch_dir.split(/[\\/]/).pop()) : null,
          t.model ? h("span", { class: "tag" }, t.model) : null),
        h("div", { class: "prompt" }, t.prompt),
        h("div", { class: "row wrap" },
          h("button", { class: "btn sm", html: icon("play") + "立即运行", onclick: async () => {
            try {
              const r = await post(`/api/tasks/${t.id}/run`);
              if (r.skipped) { toast(r.message || "这次不需要运行"); render(); return; }
              await refreshSessions();
              navigate(`#/chat/${r.session_id}`);
            } catch (e) { toast(e.message, "err"); }
          } }),
          h("button", { class: "btn sm", html: icon("edit") + "编辑", onclick: () => taskForm(t, render) }),
          h("button", { class: "btn sm", html: icon("refresh") + "运行记录", onclick: () => loadRuns() }),
          h("button", { class: "btn sm danger", html: icon("trash"), onclick: async () => {
            if (await confirmDialog(`删除任务「${t.name}」？`, { danger: true, okText: "删除" })) { await del(`/api/tasks/${t.id}`); render(); }
          } })),
        runsBox),
      h("label", { class: "switch", title: "启用/停用" }, toggle, h("span")));
    async function loadRuns() {
      if (runsBox.style.display !== "none") { runsBox.style.display = "none"; return; }
      const runs = await get(`/api/tasks/${t.id}/runs`);
      runsBox.innerHTML = "";
      runsBox.style.display = "flex";
      if (!runs.length) runsBox.append(h("div", { class: "small muted" }, "还没有运行过"));
      for (const r of runs) {
        runsBox.append(h("div", { class: "r", onclick: () => r.session_id && navigate(`#/chat/${r.session_id}`) },
          h("span", { class: `tag ${r.status === "ok" ? "ok" : r.status === "running" ? "accent" : r.status === "skipped" ? "" : "err"}` },
            { ok: "成功", running: "运行中", skipped: "跳过" }[r.status] || "失败"),
          h("span", { class: "muted" }, fmtTime(r.started_at)),
          h("span", { class: "s" }, (r.summary || "").replace(/\s+/g, " ").slice(0, 140))));
      }
    }
    return card;
  }

  render();
}
