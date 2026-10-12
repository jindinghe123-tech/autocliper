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

AutoStudio 不是“给建议”的聊天机器人，而是一个**会动手的 Agent**：它会自己查看素材、规划步骤、调用 49 个专业工具，直到把成片交到你手上。

| 能力 | 说明 |
|---|---|
| 🗣️ **语音识别字幕** | 本地 faster-whisper（免费离线，支持 GPU）或云端 API；逐词时间轴，自动断句；**Agent 会通读并修正错别字、专有名词** |
| ✍️ **字幕编辑** | Agent 可读取/修改/合并/插入/平移字幕、批量替换、翻译成双语；内置**可视化字幕编辑器**（视频实时预览） |
| 🔥 **字幕烧录** | 自定义字体/描边/背景框/弹出动画/双语高亮，自动适配横竖屏 |
| ✂️ **智能剪辑** | 精确截取、按时间段保留/删除、一键剪气口（静音）、镜头切分、拼接、变速、慢动作 |
| 🎞️ **时间线合成引擎** | 一次调用渲染完整短视频：多片段 + 40 种转场 + 调色特效 + 文字动画 + 背景音乐 + 配音 + 字幕 + 淡入淡出 |
| 🥁 **音乐卡点** | 自动分析 BPM 与节拍，让剪辑点踩在鼓点上（`beat_sync`） |
| 🪟 **分屏 / 画中画** | 左右、上下、宫格、画中画、反应视频布局，可加 Before/After 标签 |
| 🎨 **特效滤镜** | 电影感宽银幕、青橙色调、复古胶片、赛博朋克、梦幻柔光、推镜、晃动、故障风、RGB 分离、像素化、回旋播放、自定义 LUT… |
| 🎵 **音频** | 配乐（人声自动闪避 ducking）、响度标准化、降噪、人声增强、变调、音频可视化视频 |
| 🎙️ **AI 配音** | Edge 神经语音（免费）/ OpenAI 兼容 TTS / 系统语音，**自动生成对齐字幕** |
| 👀 **看懂画面** | 抽帧拼图发给模型（需模型支持图片），Agent 能自己挑精彩镜头、检查成片 |
| 🖼️ **封面 / GIF / 截图** | 一键生成带大标题的封面图 |
| 🌐 **下载与网页** | 下载 B 站 / YouTube 等视频（yt-dlp），抓取网页正文、RSS |
| ⏰ **每日自动任务** | cron 定时运行 Agent，结果推送到 **飞书 / 钉钉 / 企业微信 / Telegram / Discord / Slack / Server酱 / Bark** |
| 🧠 **长期记忆** | 记住你的偏好（字幕样式、频道风格、常用素材目录） |

## 🚀 快速开始

### 1. 安装

需要 Python 3.10+。

```bash
git clone https://github.com/jindinghe123-tech/autocliper.git
cd autocliper
pip install -e ".[all]"
```

> `[all]` 包含语音识别（faster-whisper）、AI 配音（edge-tts）、视频下载（yt-dlp）。
> 只想要核心功能：`pip install -e .`
>
> **FFmpeg**：已通过 `imageio-ffmpeg` 自带一个，开箱即用。如果你的系统已安装 ffmpeg（`winget install ffmpeg` / `brew install ffmpeg` / `apt install ffmpeg`），会优先使用系统版本。

Windows 用户也可以直接双击 `scripts/start-windows.bat`（自动创建虚拟环境、安装依赖并启动）。

### 2. 启动

```bash
autostudio
```

浏览器会自动打开 <http://127.0.0.1:8765>。

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
- `每天早上 9 点抓取 Hacker News 热门，整理成中文简报推送到我的飞书群`

## ⏰ 每日自动任务

在 **定时任务** 页面新建（或直接在对话里说“每天 xx 点帮我……”）。到点后 AutoStudio 会开启一个新对话，以无人值守模式执行指令，并把结果推送到你填写的 Webhook。

- 需要 AutoStudio 保持运行（可以开机自启，见下文）
- 错过 1 小时以内的任务会在恢复后自动补跑
- 无人值守时默认禁止执行命令行，可在任务里单独开启

<details>
<summary>开机自启（可选）</summary>

- **Windows**：`Win+R` 输入 `shell:startup`，在打开的文件夹里放一个快捷方式，目标为 `pythonw -m autostudio --no-browser`（或 `scripts\start-windows.bat`）
- **macOS**：系统设置 → 通用 → 登录项，添加一个运行 `autostudio --no-browser` 的脚本
- **Linux**：写一个 systemd user service，`ExecStart=autostudio --no-browser`

</details>

## 🧩 架构

```
autostudio/
├── agent/          # Agent 循环（模型 ↔ 工具）、运行管理（流式事件、中断、审批）、系统提示词
├── providers/      # 模型接入：ChatGPT 订阅（OAuth）/ OpenAI Responses / Chat Completions 兼容 / Anthropic
├── tools/          # 49 个工具：video · creative · subtitle · system
├── media/          # ffmpeg 封装、字幕格式与断句、语音识别、节拍检测、TTS 引擎
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

仓库地址写在 `site/index.html` 末尾脚本的 `REPO_URL` 常量里。

## 🗺️ Roadmap

- [ ] 文件夹监听触发（新视频落地即自动处理）
- [ ] 多轨时间线可视化编辑器
- [ ] AI 生图 / 生视频工具接入
- [ ] 人脸跟踪智能裁切（横转竖）
- [ ] MCP 工具扩展
- [ ] 桌面安装包（Windows / macOS）

## 📄 License

MIT
