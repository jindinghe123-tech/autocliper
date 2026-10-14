"""Thin async wrapper around the ffmpeg / ffprobe binaries."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg", ".3gp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

ENC_V = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
ENC_A = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
MP4_FLAGS = ["-movflags", "+faststart"]

_override: str = ""


class FFmpegError(RuntimeError):
    pass


def set_ffmpeg_override(path: str) -> None:
    global _override
    _override = path or ""


def ffmpeg_bin() -> str:
    if _override and Path(_override).is_file():
        return _override
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        raise FFmpegError("找不到 ffmpeg。请安装 ffmpeg（Windows: winget install ffmpeg；macOS: brew install ffmpeg），"
                          "或 pip install imageio-ffmpeg。") from e


def ffprobe_bin() -> str | None:
    found = shutil.which("ffprobe")
    if found:
        return found
    try:
        sibling = Path(ffmpeg_bin()).with_name("ffprobe" + (".exe" if IS_WIN else ""))
        return str(sibling) if sibling.is_file() else None
    except FFmpegError:
        return None


async def run_proc(args: list[str], cwd: str | Path | None = None, timeout: float | None = None,
                   on_stdout: Callable[[str], None] | None = None,
                   on_stderr: Callable[[str], None] | None = None,
                   env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a process in worker threads (works on every event loop, incl. Windows selector loops)."""
    loop = asyncio.get_running_loop()
    proc = subprocess.Popen(
        [str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        cwd=str(cwd) if cwd else None, creationflags=CREATE_NO_WINDOW, env=env,
    )
    out_lines: list[str] = []
    err_lines: list[str] = []

    def pump(stream: Any, sink: list[str], cb: Callable[[str], None] | None) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            sink.append(line)
            if len(sink) > 4000:
                del sink[:2000]
            if cb:
                loop.call_soon_threadsafe(cb, line)
        stream.close()

    threads = [threading.Thread(target=pump, args=(proc.stdout, out_lines, on_stdout), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err_lines, on_stderr), daemon=True)]
    for t in threads:
        t.start()
    try:
        code = await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        proc.kill()
        raise
    for t in threads:
        await asyncio.to_thread(t.join, 5)
    return code, "\n".join(out_lines), "\n".join(err_lines)


def _clock_to_sec(s: str) -> float:
    try:
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except ValueError:
        return 0.0


async def ffmpeg(args: list[str], *, progress: Callable[[float], None] | None = None,
                 duration: float | None = None, cwd: str | Path | None = None,
                 timeout: float | None = None) -> str:
    """Run ffmpeg with -y. `progress(fraction)` is called while encoding if `duration` is known."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-y", "-nostdin"]
    if progress and duration:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(a) for a in args]

    def on_out(line: str) -> None:
        if not (progress and duration):
            return
        if line.startswith("out_time="):
            t = _clock_to_sec(line.split("=", 1)[1])
            if t > 0:
                progress(min(0.999, t / duration))

    code, _out, err = await run_proc(cmd, cwd=cwd, timeout=timeout, on_stdout=on_out)
    if code != 0:
        tail = "\n".join([ln for ln in err.splitlines() if ln.strip()][-25:])
        raise FFmpegError(f"ffmpeg 失败 (code {code}):\n{tail}")
    return err


async def probe(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    fp = ffprobe_bin()
    info: dict[str, Any] = {"path": str(path), "size_mb": round(path.stat().st_size / 1048576, 2),
                            "duration": 0.0, "has_video": False, "has_audio": False}
    if fp:
        code, out, err = await run_proc([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                                         str(path)], timeout=60)
        if code != 0:
            raise FFmpegError(f"无法读取媒体信息：{err[-500:]}")
        data = json.loads(out or "{}")
        fmt = data.get("format") or {}
        info["duration"] = float(fmt.get("duration") or 0)
        info["format"] = fmt.get("format_name")
        if fmt.get("bit_rate"):
            info["bitrate_kbps"] = int(int(fmt["bit_rate"]) / 1000)
        for s in data.get("streams") or []:
            if s.get("codec_type") == "video" and not info["has_video"]:
                if (s.get("disposition") or {}).get("attached_pic"):
                    continue
                info["has_video"] = True
                info["video_codec"] = s.get("codec_name")
                w, h = int(s.get("width") or 0), int(s.get("height") or 0)
                rot = 0
                for sd in s.get("side_data_list") or []:
                    if "rotation" in sd:
                        rot = int(sd["rotation"])
                rot = rot or int((s.get("tags") or {}).get("rotate") or 0)
                if abs(rot) in (90, 270):
                    w, h = h, w
                info["width"], info["height"] = w, h
                fr = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
                try:
                    num, den = fr.split("/")
                    info["fps"] = round(float(num) / float(den), 3) if float(den) else 0
                except (ValueError, ZeroDivisionError):
                    info["fps"] = 0
                if not info["duration"] and s.get("duration"):
                    info["duration"] = float(s["duration"])
            elif s.get("codec_type") == "audio" and not info["has_audio"]:
                info["has_audio"] = True
                info["audio_codec"] = s.get("codec_name")
                info["sample_rate"] = int(s.get("sample_rate") or 0)
                info["channels"] = s.get("channels")
    else:
        code, _o, err = await run_proc([ffmpeg_bin(), "-hide_banner", "-i", str(path)], timeout=60)
        m = re.search(r"Duration: (\d+:\d+:\d+\.\d+)", err)
        if m:
            info["duration"] = _clock_to_sec(m.group(1))
        vm = re.search(r"Stream #\S+.*?Video: (\w+).*?, (\d{2,5})x(\d{2,5})", err)
        if vm and "attached pic" not in vm.group(0):
            info.update(has_video=True, video_codec=vm.group(1), width=int(vm.group(2)), height=int(vm.group(3)))
            fm = re.search(r"([\d.]+) fps", err)
            info["fps"] = float(fm.group(1)) if fm else 0
        am = re.search(r"Stream #\S+.*?Audio: (\w+)(?:.*?(\d+) Hz)?", err)
        if am:
            info.update(has_audio=True, audio_codec=am.group(1), sample_rate=int(am.group(2) or 0))
    if path.suffix.lower() in IMAGE_EXTS:
        info["is_image"] = True
        info["duration"] = 0.0
    info["duration"] = round(info["duration"], 3)
    return info


_TIME_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:[.,]\d+)?)$")


def parse_time(v: Any, default: float | None = None) -> float | None:
    """Accept 12.5 / "12.5" / "1:23.4" / "01:02:03,500" / "90s" / "2m"."""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    try:
        if s.endswith("ms"):
            return float(s[:-2]) / 1000
        if s.endswith("s"):
            return float(s[:-1])
        if s.endswith("m") and s[:-1].replace(".", "", 1).isdigit():
            return float(s[:-1]) * 60
        return float(s)
    except ValueError:
        pass
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(f"无法解析时间：{v}")
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + float(m.group(3).replace(",", "."))


def fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def filter_escape_path(p: str | Path) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter argument."""
    s = str(Path(p).resolve()).replace("\\", "/")
    s = s.replace(":", r"\:").replace("'", r"\'")
    return f"'{s}'"


def filter_escape_text(t: str) -> str:
    return t.replace("\\", r"\\\\").replace("'", r"\'").replace(":", r"\:").replace("%", r"\%").replace(",", r"\,")


def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if Path(p).is_file():
            return p
    return None


def default_font_file(bold: bool = False) -> str | None:
    if IS_WIN:
        fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        names = (["msyhbd.ttc", "simhei.ttf"] if bold else []) + ["msyh.ttc", "msyh.ttf", "simhei.ttf", "simsun.ttc",
                                                                 "arial.ttf"]
        return _first_existing([os.path.join(fonts, n) for n in names])
    if IS_MAC:
        return _first_existing(["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
                                "/System/Library/Fonts/Hiragino Sans GB.ttc", "/Library/Fonts/Arial Unicode.ttf",
                                "/System/Library/Fonts/Helvetica.ttc"])
    return _first_existing([
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ])


# Font families a video style can ask for → (Windows files, macOS files, Linux files, name for ASS subtitles).
FONT_FAMILIES: dict[str, tuple[list[str], list[str], list[str], str]] = {
    "黑体": (["simhei.ttf", "msyhbd.ttc"], ["/System/Library/Fonts/STHeiti Medium.ttc"],
             ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"], "SimHei"),
    "雅黑": (["msyhbd.ttc", "msyh.ttc"], ["/System/Library/Fonts/PingFang.ttc"],
             ["/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"], "Microsoft YaHei"),
    "细体": (["msyhl.ttc", "Dengl.ttf", "msyh.ttc"], ["/System/Library/Fonts/PingFang.ttc"],
             ["/usr/share/fonts/opentype/noto/NotoSansCJK-Light.ttc"], "Microsoft YaHei Light"),
    "等线": (["Dengb.ttf", "Deng.ttf", "msyh.ttc"], ["/System/Library/Fonts/PingFang.ttc"],
             ["/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"], "DengXian"),
    "楷体": (["simkai.ttf"], ["/System/Library/Fonts/Supplemental/Kaiti.ttc", "/Library/Fonts/Kaiti.ttc",
                               "/System/Library/Fonts/STKaiti.ttc"], ["/usr/share/fonts/truetype/arphic/ukai.ttc"], "KaiTi"),
    "宋体": (["simsun.ttc"], ["/System/Library/Fonts/Supplemental/Songti.ttc", "/System/Library/Fonts/Songti.ttc"],
             ["/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"], "SimSun"),
    "仿宋": (["simfang.ttf", "simsun.ttc"], ["/System/Library/Fonts/Supplemental/Songti.ttc"],
             ["/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"], "FangSong"),
}
FONT_ALIASES = {"sans": "黑体", "bold": "黑体", "heiti": "黑体", "simhei": "黑体", "yahei": "雅黑", "微软雅黑": "雅黑",
                "light": "细体", "thin": "细体", "dengxian": "等线", "kaiti": "楷体", "kai": "楷体", "手写": "楷体",
                "serif": "宋体", "songti": "宋体", "simsun": "宋体", "fangsong": "仿宋"}


def _family(name: str | None) -> str | None:
    n = (name or "").strip()
    return n if n in FONT_FAMILIES else FONT_ALIASES.get(n.lower())


def font_file(name: str | None, bold: bool = True) -> str | None:
    """Font file for a family name (黑体/楷体/宋体/仿宋/等线/雅黑/细体 or an alias); falls back to the default."""
    fam = _family(name)
    if fam:
        win, mac, linux, _ = FONT_FAMILIES[fam]
        if IS_WIN:
            d = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
            found = _first_existing([os.path.join(d, n) for n in win])
        else:
            found = _first_existing(mac if IS_MAC else linux)
        if found:
            return found
    elif name:
        from .fonts import FONT_DIR, lookup

        got = lookup(name)
        if got:
            return str(got[0])
        dirs = [FONT_DIR, Path(fonts_dir() or "."), Path(os.environ.get("LOCALAPPDATA", ".")) / "Microsoft/Windows/Fonts"]
        for d in dirs:
            for cand in (name, f"{name}.ttf", f"{name}.ttc", f"{name}.otf"):
                if (d / cand).is_file():
                    return str(d / cand)
        if Path(name).is_file():
            return name
    return default_font_file(bold)


def font_name(name: str | None) -> str:
    """Family name for ASS subtitles (libass looks fonts up by name)."""
    fam = _family(name)
    if fam and font_file(fam) != default_font_file(True):
        return FONT_FAMILIES[fam][3]
    if name and not fam:
        from .fonts import lookup

        got = lookup(name)
        return got[1] if got else name
    return default_font_name()


def fonts_dir_for(name: str | None) -> str | None:
    """libass looks fonts up by name: point it at our download folder when the subtitle font lives there."""
    from .fonts import FONT_DIR, lookup

    return str(FONT_DIR) if name and lookup(name) else fonts_dir()


def default_font_name() -> str:
    if IS_WIN:
        return "Microsoft YaHei"
    if IS_MAC:
        return "PingFang SC"
    return "Noto Sans CJK SC"


def fonts_dir() -> str | None:
    if IS_WIN:
        return os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for d in ("/System/Library/Fonts", "/usr/share/fonts"):
        if Path(d).is_dir():
            return d
    return None


def even(n: float) -> int:
    n = int(round(n))
    return n - (n % 2)


ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350),
           "3:4": (1080, 1440), "4:3": (1440, 1080), "21:9": (2520, 1080)}


def parse_size(size: str | None, aspect: str | None = None, fallback: tuple[int, int] = (1920, 1080)) -> tuple[int, int]:
    if size:
        m = re.match(r"^\s*(\d+)\s*[x×*:]\s*(\d+)\s*$", str(size))
        if m and int(m.group(1)) > 32:
            return even(int(m.group(1))), even(int(m.group(2)))
        if str(size) in ASPECTS:
            return ASPECTS[str(size)]
    if aspect and aspect in ASPECTS:
        return ASPECTS[aspect]
    return fallback


def fit_filter(w: int, h: int, mode: str = "crop", bg: str = "black") -> str:
    """Filter chain (single input/output) that fits a video into WxH."""
    if mode == "pad":
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={bg},setsar=1")
    if mode == "stretch":
        return f"scale={w}:{h},setsar=1"
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"


def blur_fit_graph(inp: str, out: str, w: int, h: int, tag: str = "bf") -> str:
    """Filtergraph fragment: fit `inp` into WxH over a blurred, zoomed copy of itself."""
    return (f"[{inp}]split=2[{tag}a][{tag}b];"
            f"[{tag}a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=luma_radius=30:luma_power=2,"
            f"eq=brightness=-0.06[{tag}bg];"
            f"[{tag}b]scale={w}:{h}:force_original_aspect_ratio=decrease[{tag}fg];"
            f"[{tag}bg][{tag}fg]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]")


def atempo_chain(factor: float) -> str:
    parts = []
    f = float(factor)
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.5f}")
    return ",".join(parts)


def color_hex_to_ffmpeg(c: str | None, default: str = "white") -> str:
    if not c:
        return default
    c = str(c).strip()
    if re.match(r"^#?[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$", c):
        return "0x" + c.lstrip("#")
    return c


async def capabilities() -> dict[str, Any]:
    try:
        exe = ffmpeg_bin()
    except FFmpegError as e:
        return {"ffmpeg": None, "error": str(e)}
    code, out, _ = await run_proc([exe, "-hide_banner", "-filters"], timeout=30)
    _c2, enc, _ = await run_proc([exe, "-hide_banner", "-encoders"], timeout=30)
    _c3, ver, _ = await run_proc([exe, "-version"], timeout=30)
    return {
        "ffmpeg": exe,
        "ffprobe": ffprobe_bin(),
        "version": (ver.splitlines() or [""])[0],
        "libass": " ass " in out or "\n ... ass " in out or re.search(r"\sass\s", out) is not None,
        "drawtext": "drawtext" in out,
        "xfade": "xfade" in out,
        "libx264": "libx264" in enc,
    }
