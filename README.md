<div align="center">

# 🎬 AutoStudio

**开源的 AI 视频创作 Agent —— 用一句话剪视频、做字幕、配乐、加特效，还能每天自动干活。**

用你的 **ChatGPT 订阅** 登录即可使用，或接入任意大模型 API（Claude / Gemini / DeepSeek / 通义千问 / Kimi / 智谱 / 豆包 / OpenRouter / Ollama …）

`Python 3.10+` · `FFmpeg` · `faster-whisper` · `MIT License`

🌐 官网：<https://autostudio.jindinghe123.workers.dev>

</div>

---

## ✨ 它能做什么

> “把这段口播的停顿剪掉，配上字幕，转成竖屏，再加个吸引人的标题和背景音乐。”

AutoStudio 不是“给建议”的聊天机器人，而是一个**会动手的 Agent**：它会自己查看素材、规划步骤、调用 69 个专业工具，直到把成片交到你手上。

| 能力 | 说明 |
|---|---|
| 🎬 **Agent 亲手剪辑** | 像剪辑师一样在时间线上工作：画面主轨、任意多层文字（任意字体）、画中画/贴图、多条音轨（配音/音乐/音效各自精确到时间点），逐条增删改、渲染预览、回看、返工。每条视频先写创意方案，形式和视觉语言按题材设计，没有模板 |
| ✅ **交付关卡** | 只说“选个热搜做成短视频”也要交付完整成品：工作清单实时可见；最终版必须是最新时间线、必须被真正看过，并自动检查黑屏、无声、音量（自动标准化到 -14 LUFS）、画面里有没有网址或来源 |
| 🔎 **自动找素材** | Agent 自己搜索并下载可商用的实拍画面、图片、背景音乐、音效（Pexels / Pixabay / Freesound / Jamendo / Openverse / Wikimedia），看缩略图挑画面，自动记录授权和署名 |
| 🗣️ **语音识别字幕** | 本地 faster-whisper（免费离线，支持 GPU）或云端 API；逐词时间轴，自动断句；**Agent 会通读并修正错别字、专有名词** |
| ✍️ **字幕编辑** | Agent 可读取/修改/合并/插入/平移字幕、批量替换、翻译成双语；内置**可视化字幕编辑器**（视频实时预览） |
| 🔥 **字幕烧录** | 自定义字体/描边/背景框/弹出动画/双语高亮，自动适配横竖屏 |
| ✂️ **智能剪辑** | 精确截取、按时间段保留/删除、一键剪气口（静音）、镜头切分、拼接、变速、慢动作 |
| 🎞️ **时间线合成引擎** | 一次调用渲染完整短视频：多片段 + 40 种转场 + 调色特效 + 文字动画 + 背景音乐 + 配音 + 字幕 + 淡入淡出 |
| 🥁 **音乐卡点** | 自动分析 BPM 与节拍，让剪辑点踩在鼓点上（`beat_sync`） |
| 🪟 **分屏 / 画中画** | 左右、上下、宫格、画中画、反应视频布局，可加 Before/After 标签 |
| 🎨 **特效滤镜** | 电影感宽银幕、青橙色调、复古胶片、赛博朋克、梦幻柔光、推镜、晃动、故障风、RGB 分离、像素化、回旋播放、自定义 LUT… |
| 🎵 **音频** | 配乐（人声自动闪避 ducking）、响度标准化、降噪、人声增强、变调、音频可视化视频 |
| 🎙️ **AI 配音** | Edge 神经语音 / **离线神经配音（免费、不联网、100 个中文音色）** / 火山引擎 / 声音克隆 / OpenAI 兼容 TTS，**自动生成对齐字幕** |
| 👀 **看懂画面** | 抽帧拼图发给模型（需模型支持图片），Agent 能自己挑精彩镜头、检查成片 |
| 🖼️ **封面 / GIF / 截图** | 一键生成带大标题的封面图 |
| 🌐 **下载与网页** | 下载 B 站 / YouTube 等视频（yt-dlp），抓取网页正文、RSS |
| 📮 **一键发布** | 发布到 **小红书**（视频 / 图文笔记）和 **YouTube**（含 Shorts），每个平台都能添加**多个账号**，一次发到多个账号；定时任务产出的内容先进「待确认」，点一下才发 |
| ⏰ **每日自动任务** | cron 定时运行 Agent，结果推送到 **飞书 / 钉钉 / 企业微信 / Telegram / Discord / Slack / Server酱 / Bark** |
| 🧠 **长期记忆** | 记住你的偏好（字幕样式、频道风格、常用素材目录） |

## 🚀 快速开始

### 1. 下载并启动（不用敲命令）

1. 下载 **[AutoStudio.zip](https://autostudio.jindinghe123.workers.dev/AutoStudio.zip)**（或本页 Code → Download ZIP），解压
2. 启动：
   - **Windows**：双击 `AutoStudio.bat`
   - **macOS**：右键 `AutoStudio.command` → 打开
   - **Linux**：`./AutoStudio.command`
3. 首次运行会自动检查并安装所需的一切：Python 3.12、ffmpeg、语音识别、AI 配音、视频下载。装好后浏览器自动打开 <http://127.0.0.1:8765>，以后双击就能秒开

不需要提前装 Python，也不需要管理员权限。所有东西都装在程序文件夹的 `.runtime/` 和 `.venv/` 里，删掉文件夹就彻底卸载。

> - 中国大陆（系统时区为中国标准时间）会自动优先使用国内镜像（清华 / 阿里云 PyPI、npmmirror），官方源不通时也会自动切换。设置环境变量 `AUTOSTUDIO_MIRROR=1` / `0` 可以强制开启 / 关闭
> - 开了系统代理（Clash、v2rayN 等）时，下载会自动走代理
> - 更新：下载新版覆盖解压即可，启动时会自动补装新依赖
> - Windows 提示“已保护你的电脑”：点 **更多信息 → 仍要运行**

### 2. 用命令行安装（开发者）

需要 Python 3.10+。

```bash
git clone https://github.com/jindinghe123-tech/autocliper.git
cd autocliper
pip install -e ".[all]"
autostudio
```

> `[all]` 包含语音识别（faster-whisper）、AI 配音（edge-tts）、视频下载（yt-dlp）。
> 只想要核心功能：`pip install -e .`
>
> **FFmpeg**：已通过 `imageio-ffmpeg` 自带一个，开箱即用。如果你的系统已安装 ffmpeg（`winget install ffmpeg` / `brew install ffmpeg` / `apt install ffmpeg`），会优先使用系统版本。

### 3. 连接大模型（二选一）

**方式一：ChatGPT 订阅（推荐 Plus / Pro 用户）**

设置 → 模型 → **登录 ChatGPT**，在弹出的 OpenAI 页面登录即可。对话消耗的是你的订阅额度，不需要 API Key。
已经用过 Codex CLI（`codex login`）？点 **从 Codex CLI 导入登录** 一键复用。

**方式二：API Key（按 token 计费）**

设置 → 模型 → **添加提供方**，选择预设并填入 Key：

| 提供方 | 协议 | 备注 |
|---|---|---|
| OpenAI | Responses API | |
| Anthropic Claude | Messages API | |
| Google Gemini | OpenAI 兼容 | |
| DeepSeek / 通义千问 / Kimi / 智谱 GLM / 豆包 / 硅基流动 | OpenAI 兼容 | 国内直连 |
| OpenRouter | OpenAI 兼容 | 一个 Key 用几百个模型 |
| Ollama | OpenAI 兼容 | 本地模型，无需 Key |
| 自定义 | OpenAI 兼容 | 任意兼容接口 |

> 💡 想让 Agent “看”视频画面，请选择支持图片输入的模型，并在提供方设置里勾选“支持图片输入”。

### 4. 开始创作

把视频拖进对话框，然后说出你想要的效果，例如：

- `给这个视频生成中文字幕，校对后用黄色描边字烧录上去`
- `把这个 20 分钟的直播回放剪成 60 秒竖屏精华，开头放一个钩子标题，配上卡点音乐`
- `把这两个视频做成左右分屏对比，标上 Before / After`
- `用这 8 张照片和下面这段文案做一个带 AI 配音和字幕的竖屏短视频`
- `下载这个 B 站视频，把字幕翻译成英文，做成双语字幕版`
- `请选择一个热搜榜热门话题然后做成一个短视频`
- `每天早上 9 点抓取 Hacker News 热门，整理成中文简报推送到我的飞书群`
- `把这个视频剪成竖屏，写好文案，发到我的两个小红书号和 YouTube 频道`

## 🔎 素材库

Agent 缺画面、缺音乐、缺音效时会自己去找：`asset_search` 返回带编号的缩略图拼图让它“看图挑选”，`asset_download` 下载到工作区 `assets/`，并把作者、授权、来源记到 `assets/CREDITS.md`。默认只用**可商用、可修改**的授权；需要署名的素材会写进发布文案。

| 素材库 | 内容 | 需要 key |
|---|---|---|
| Openverse | CC 图片、Jamendo 音乐、Freesound 音效 | 不需要（匿名每天约 200 次） |
| Wikimedia Commons | 自由授权的视频和图片 | 不需要 |
| Pexels / Pixabay | 高质量实拍视频和照片，免费商用、无需署名 | 免费申请 |
| Freesound | 60 万+ 音效 | 免费申请 |
| Jamendo | 60 万+ 首独立音乐 | 免费申请 |

在 **设置 → 素材** 填 key（每个一两分钟就能申请到），实拍视频素材会多很多。

## 🗣️ 离线配音

微软 Edge 免费配音从 2026-10 起在部分地区不可用，而很多电脑没装中文系统语音。AutoStudio 内置了离线神经网络配音（Kokoro，通过 sherpa-onnx 在本机运行）：免费、不联网、55 个中文女声 + 45 个中文男声。其它引擎不可用时会自动改用它，第一次使用会下载约 150MB 的模型（国内自动走镜像），也可以在 **设置 → 语音** 提前下载。

## 📮 发布到小红书 / YouTube

左侧 **发布** 页面管理账号，每个平台都可以添加多个：

- **小红书**：点「添加小红书账号」，在弹出的浏览器窗口里用小红书 App 扫码登录创作服务平台。每个账号用独立的浏览器配置，登录信息只保存在本机。支持视频笔记和 1~18 张图的图文笔记；可以先点「试运行」，它会上传测试视频、填好表单，但不点发布。
  小红书没有面向个人的发布接口，这里是自动操作网页完成的：页面改版可能导致失效（失败时会保存截图），遇到安全验证需要你在窗口里手动完成，也请控制发布频率。
- **YouTube**：走官方 YouTube Data API。开源软件无法内置统一的 Google 授权，所以第一次需要用你自己的 Google 账号创建一个免费的 OAuth 客户端（页面里有分步引导，约 5 分钟），之后每个频道（包括品牌账号）登录一次即可。
  注意：未经 YouTube API 审核的 Google Cloud 项目，通过 API 上传的视频会被锁定为「私享」，需要在 YouTube 工作室手动公开，或[提交审核](https://support.google.com/youtube/contact/yt_api_form)后自动公开；默认配额每天也只够上传少量视频。

在对话里直接说「发到我的小红书和 YouTube」即可；Agent 会按平台分别写标题和文案。发布前默认会征求你的同意（发布页可改）。
定时任务产出的内容默认进入「待确认」，任务完成后推送里会提醒你；在任务里勾选「直接发出」才会全自动发布。

## ⏰ 每日自动任务

在 **定时任务** 页面新建（或直接在对话里说“每天 xx 点帮我……”）。到点后 AutoStudio 会开启一个新对话，以无人值守模式执行指令，并把结果推送到你填写的 Webhook。

- 需要 AutoStudio 保持运行（可以开机自启，见下文）
- 错过 1 小时以内的任务会在恢复后自动补跑
- 无人值守时默认禁止执行命令行，可在任务里单独开启

<details>
<summary>开机自启（可选）</summary>

- **Windows**：`Win+R` 输入 `shell:startup`，在打开的文件夹里放一个 `AutoStudio.bat` 的快捷方式，目标末尾加上 ` --no-browser`
- **macOS**：系统设置 → 通用 → 登录项，添加一个运行 `autostudio --no-browser` 的脚本
- **Linux**：写一个 systemd user service，`ExecStart=autostudio --no-browser`

</details>

## 🧩 架构

```
autostudio/
├── agent/          # Agent 循环（模型 ↔ 工具）、运行管理（流式事件、中断、审批）、系统提示词
├── providers/      # 模型接入：ChatGPT 订阅（OAuth）/ OpenAI Responses / Chat Completions 兼容 / Anthropic
├── timeline.py     # 时间线工程：数据模型、检查、字幕、观看记录
├── tools/          # 69 个工具：video · creative · editing（时间线剪辑 / 交付关卡 / 字体）· assets（素材）· research（热榜）· subtitle · social · system
├── media/          # ffmpeg 封装、字幕格式与断句、语音识别、节拍检测、TTS 引擎
├── social/         # 社交发布：多账号、待确认队列、YouTube API、小红书网页自动化
├── scheduler.py    # cron 定时任务 + Webhook 推送
├── server.py       # FastAPI：REST + SSE
└── web/            # 前端（原生 JS，无需构建）
```

**添加一个新工具**只需要一个装饰器：

```python
from autostudio.media import ffmpeg as ff
from autostudio.tools.registry import tool, obj, s, ToolContext

@tool("video_mirror", "水平镜像视频", obj(["input"], input=s("输入视频")), category="video")
async def video_mirror(ctx: ToolContext, input: str):
    src = ctx.resolve(input, must_exist=True)
    out = ctx.out_path(src, "_mirror")
    await ff.ffmpeg(["-i", str(src), "-vf", "hflip", str(out)], progress=ctx.progress_cb())
    return {"ok": True, "output": ctx.rel(out), "files": [str(out)]}
```

返回值里的 `files` 会自动在界面里预览，`images` 会作为图片发给模型。

## 🔒 安全与隐私

- 服务默认只监听 `127.0.0.1`，并拦截跨站请求（防止网页偷偷调用本地 Agent）
- API Key 和 ChatGPT 令牌只保存在本机 `~/.autostudio/`，只会发送给对应的服务商
- 执行命令行需要你逐次批准（可在设置里调整）；Agent 不会删除文件（只会移到工作区 `.trash/`）
- 不要用 `--host 0.0.0.0` 把服务暴露到公网

## ⚠️ 关于 ChatGPT 订阅登录

“登录 ChatGPT”使用与 OpenAI 开源的 [Codex CLI](https://github.com/openai/codex) 相同的 OAuth 流程，调用 ChatGPT 的 Codex 后端。这不是公开文档化的 API，OpenAI 可能随时调整导致暂时不可用；请仅用于你自己的账户，并遵守 [OpenAI 使用条款](https://openai.com/policies/terms-of-use)。本项目与 OpenAI 无关联。

## 🛠️ 开发

```bash
pip install -e ".[all,dev]"
pytest
ruff check autostudio
python -m autostudio --no-browser   # 修改前端后刷新浏览器即可，无需构建
```

数据目录可用环境变量 `AUTOSTUDIO_HOME` 指定（便于测试）。

### 项目官网

官网是 `site/` 下的静态页面，部署在 Cloudflare：<https://autostudio.jindinghe123.workers.dev>。修改后在项目根目录运行即可更新：

```bash
npx wrangler deploy
```

仓库地址写在 `site/index.html` 末尾脚本的 `REPO_URL` 常量里。官网上的下载包 `AutoStudio.zip` 会在每次部署时由 `git archive` 从**最新提交**自动打包（见 `wrangler.jsonc`），所以先提交再部署。

## 🗺️ Roadmap

- [ ] 文件夹监听触发（新视频落地即自动处理）
- [ ] 多轨时间线可视化编辑器
- [ ] AI 生图 / 生视频工具接入
- [ ] 人脸跟踪智能裁切（横转竖）
- [ ] 更多发布平台（B 站、抖音、视频号）
- [ ] MCP 工具扩展
- [ ] 桌面安装包（Windows / macOS）

## 📄 License

MIT
